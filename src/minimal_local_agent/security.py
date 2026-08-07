"""Filesystem boundary checks shared by all local tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceSecurityError(ValueError):
    """Raised when a requested path crosses the configured workspace boundary."""


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
        value = resolved.relative_to(self.root).as_posix()
        return value or "."

    @staticmethod
    def validate_pattern(pattern: str) -> None:
        path = Path(pattern)
        if not pattern.strip():
            raise WorkspaceSecurityError("Glob pattern cannot be empty")
        if path.is_absolute() or ".." in path.parts:
            raise WorkspaceSecurityError("Glob pattern cannot leave the workspace")
