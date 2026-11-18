"""工作区文件系统：路径边界检查与受限的读写操作（移植自 v1 security/workspace）。"""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

EXCLUDED_PARTS = {
    ".git",
    ".mla",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class WorkspaceSecurityError(ValueError):
    """请求的路径越过了工作区边界。"""


@dataclass(slots=True)
class WorkspaceGuard:
    root: Path

    def __post_init__(self) -> None:
        self.root = self.root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(
        self,
        relative_path: str | Path,
        *,
        must_exist: bool = False,
        allow_root: bool = False,
    ) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute():
            raise WorkspaceSecurityError("Absolute paths are not allowed")
        candidate = (self.root / raw).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise WorkspaceSecurityError("Path escapes the configured workspace")
        if candidate == self.root and not allow_root:
            raise WorkspaceSecurityError("A file path is required")
        if must_exist and not candidate.exists():
            raise FileNotFoundError(f"Path does not exist: {relative_path}")
        return candidate

    def relative(self, path: Path) -> str:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkspaceSecurityError("Path escapes the configured workspace")
        return resolved.relative_to(self.root).as_posix() or "."

    @staticmethod
    def validate_pattern(pattern: str) -> None:
        path = Path(pattern)
        if not pattern.strip():
            raise WorkspaceSecurityError("Glob pattern cannot be empty")
        if path.is_absolute() or ".." in path.parts:
            raise WorkspaceSecurityError("Glob pattern cannot leave the workspace")


def _visible_diff_lines(text: str) -> list[str]:
    """把只改换行符的差异显式标出来，避免审批时看不见。"""

    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.endswith("\r\n"):
            lines.append(f"{line[:-2]} ⟪CRLF⟫")
        elif line.endswith("\n"):
            lines.append(line[:-1])
        elif line.endswith("\r"):
            lines.append(f"{line[:-1]} ⟪CR⟫")
        else:
            lines.append(f"{line} ⟪no newline at end of file⟫")
    return lines


def unified_diff(path: str, before: str | None, after: str | None) -> str:
    return "\n".join(
        difflib.unified_diff(
            _visible_diff_lines(before or ""),
            _visible_diff_lines(after or ""),
            fromfile=f"a/{path}" if before is not None else "/dev/null",
            tofile=f"b/{path}" if after is not None else "/dev/null",
            lineterm="",
        )
    )


@dataclass(slots=True)
class Workspace:
    """所有文件操作的唯一入口；工具和恢复逻辑都通过它访问磁盘。"""

    guard: WorkspaceGuard
    max_file_bytes: int = 200_000
    max_list_results: int = 200
    max_search_results: int = 100
    max_search_files: int = 500

    @classmethod
    def at(cls, root: str | Path, **limits: int) -> Workspace:
        return cls(WorkspaceGuard(Path(root)), **limits)

    @property
    def root(self) -> Path:
        return self.guard.root

    def _excluded(self, path: Path) -> bool:
        relative = path.resolve(strict=False).relative_to(self.guard.root)
        return any(part in EXCLUDED_PARTS for part in relative.parts)

    def list_files(self, path: str = ".", pattern: str = "**/*") -> list[str]:
        self.guard.validate_pattern(pattern)
        directory = self.guard.resolve(path, must_exist=True, allow_root=True)
        if not directory.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")
        results: list[str] = []
        for item in sorted(directory.glob(pattern), key=lambda v: v.as_posix()):
            resolved = item.resolve(strict=False)
            if not resolved.is_relative_to(self.guard.root) or self._excluded(resolved):
                continue
            label = self.guard.relative(resolved)
            results.append(f"{label}/" if item.is_dir() else label)
            if len(results) >= self.max_list_results:
                break
        return results

    def read_file(self, path: str) -> str:
        target = self.guard.resolve(path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(f"Not a file: {path}")
        size = target.stat().st_size
        if size > self.max_file_bytes:
            raise ValueError(
                f"File is {size} bytes; limit is {self.max_file_bytes} bytes"
            )
        data = target.read_bytes()
        if b"\x00" in data:
            raise ValueError("Binary files are not supported")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("File is not valid UTF-8 text") from exc

    def read_optional(self, path: str) -> str | None:
        """文件不存在时返回 None；用于对账和补偿。"""

        target = self.guard.resolve(path)
        if not target.exists():
            return None
        return self.read_file(path)

    def search_text(
        self,
        query: str,
        path: str = ".",
        pattern: str = "**/*",
        *,
        case_sensitive: bool = False,
    ) -> list[dict[str, str | int]]:
        if not query:
            raise ValueError("Search query cannot be empty")
        self.guard.validate_pattern(pattern)
        directory = self.guard.resolve(path, must_exist=True, allow_root=True)
        if not directory.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")
        needle = query if case_sensitive else query.casefold()
        matches: list[dict[str, str | int]] = []
        scanned = 0
        for candidate in sorted(directory.glob(pattern), key=lambda v: v.as_posix()):
            if scanned >= self.max_search_files:
                break
            resolved = candidate.resolve(strict=False)
            if (
                not candidate.is_file()
                or not resolved.is_relative_to(self.guard.root)
                or self._excluded(resolved)
            ):
                continue
            scanned += 1
            try:
                if resolved.stat().st_size > self.max_file_bytes:
                    continue
                data = resolved.read_bytes()
                if b"\x00" in data:
                    continue
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                haystack = line if case_sensitive else line.casefold()
                if needle in haystack:
                    matches.append(
                        {
                            "path": self.guard.relative(resolved),
                            "line": number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= self.max_search_results:
                        return matches
        return matches

    def write_file(self, path: str, content: str) -> int:
        """原子写入：先写临时文件、fsync，再 rename 覆盖。"""

        target = self.guard.resolve(path)
        if target.exists() and target.is_dir():
            raise IsADirectoryError(f"Cannot replace a directory: {path}")
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise ValueError(
                f"Content is {len(encoded)} bytes; limit is {self.max_file_bytes} bytes"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        # 创建父目录后再解析一次，防止新出现的符号链接逃出工作区。
        target = self.guard.resolve(path)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", delete=False, dir=target.parent, prefix=".mla-"
            ) as handle:
                temporary = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary and Path(temporary).exists():
                Path(temporary).unlink()
        return len(encoded)

    def restore_file(self, path: str, content: str | None) -> None:
        """恢复快照；content 为 None 表示文件原本不存在，需要删除。"""

        target = self.guard.resolve(path)
        if content is None:
            if target.is_file():
                target.unlink()
            elif target.exists():
                raise IsADirectoryError(f"Cannot remove a directory: {path}")
            return
        self.write_file(path, content)


__all__ = [
    "Workspace",
    "WorkspaceGuard",
    "WorkspaceSecurityError",
    "sha256_text",
    "unified_diff",
]
