"""Pure local workspace operations exposed to the model as tools."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from minimal_local_agent.security import WorkspaceGuard, WorkspaceSecurityError

EXCLUDED_PARTS = {
    ".git",
    ".minimal-local-agent",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


@dataclass(slots=True)
class WorkspaceTools:
    guard: WorkspaceGuard
    max_file_bytes: int = 200_000
    max_list_results: int = 200
    max_search_results: int = 100
    max_search_files: int = 500

    def _excluded(self, path: Path) -> bool:
        relative = path.resolve(strict=False).relative_to(self.guard.root)
        return any(part in EXCLUDED_PARTS for part in relative.parts)

    def list_files(self, path: str = ".", pattern: str = "**/*") -> list[str]:
        self.guard.validate_pattern(pattern)
        directory = self.guard.resolve(path, must_exist=True, allow_root=True)
        if not directory.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")

        results: list[str] = []
        for item in sorted(directory.glob(pattern), key=lambda value: value.as_posix()):
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
        for candidate in sorted(
            directory.glob(pattern), key=lambda value: value.as_posix()
        ):
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

            for line_number, line in enumerate(text.splitlines(), start=1):
                haystack = line if case_sensitive else line.casefold()
                if needle in haystack:
                    matches.append(
                        {
                            "path": self.guard.relative(resolved),
                            "line": line_number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= self.max_search_results:
                        return matches
        return matches

    def write_file(self, path: str, content: str, *, overwrite: bool = False) -> int:
        target = self.guard.resolve(path)
        if target.exists() and target.is_dir():
            raise IsADirectoryError(f"Cannot replace a directory: {path}")
        if target.exists() and not overwrite:
            raise FileExistsError(
                f"File already exists: {path}. Set overwrite=true only when "
                "replacement is intended."
            )

        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise ValueError(
                f"Content is {len(encoded)} bytes; limit is {self.max_file_bytes} bytes"
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        # Resolve again after creating parents so a new symlink is checked.
        target = self.guard.resolve(path)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", delete=False, dir=target.parent, prefix=".mla-"
            ) as handle:
                temporary_name = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        finally:
            if temporary_name and Path(temporary_name).exists():
                Path(temporary_name).unlink()
        return len(encoded)


__all__ = ["WorkspaceSecurityError", "WorkspaceTools"]
