from pathlib import Path

import pytest

from minimal_local_agent.security import WorkspaceGuard, WorkspaceSecurityError


def test_resolves_paths_inside_workspace(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path / "workspace")

    target = guard.resolve("notes/today.md")

    assert target == (tmp_path / "workspace/notes/today.md").resolve()


def test_rejects_parent_traversal(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path / "workspace")

    with pytest.raises(WorkspaceSecurityError, match="escapes"):
        guard.resolve("../secret.txt")


def test_rejects_absolute_path(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path / "workspace")

    with pytest.raises(WorkspaceSecurityError, match="Absolute"):
        guard.resolve((tmp_path / "outside.txt").resolve())


def test_rejects_unsafe_glob(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path / "workspace")

    with pytest.raises(WorkspaceSecurityError, match="cannot leave"):
        guard.validate_pattern("../*.txt")
