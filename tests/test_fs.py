from pathlib import Path

import pytest

from minimal_local_agent.tools.fs import Workspace, WorkspaceGuard, WorkspaceSecurityError


def test_guard_resolves_inside_and_rejects_escape(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path / "ws")
    assert guard.resolve("a/b.txt") == (tmp_path / "ws/a/b.txt").resolve()
    with pytest.raises(WorkspaceSecurityError, match="escapes"):
        guard.resolve("../secret.txt")
    with pytest.raises(WorkspaceSecurityError, match="Absolute"):
        guard.resolve((tmp_path / "x").resolve())
    with pytest.raises(WorkspaceSecurityError, match="cannot leave"):
        guard.validate_pattern("../*.txt")


def test_guard_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = Workspace.at(tmp_path / "ws")
    (ws.root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceSecurityError):
        ws.write_file("link/pwned.txt", "x")
    assert not (outside / "pwned.txt").exists()


def test_write_read_list_search(tmp_path: Path) -> None:
    ws = Workspace.at(tmp_path / "ws")
    assert ws.write_file("notes/hello.txt", "Hello\nLocal Agent\n") == 18
    assert ws.read_file("notes/hello.txt") == "Hello\nLocal Agent\n"
    assert "notes/hello.txt" in ws.list_files()
    assert ws.search_text("local agent")[0] == {
        "path": "notes/hello.txt",
        "line": 2,
        "text": "Local Agent",
    }
    assert ws.read_optional("missing.txt") is None


def test_rejects_binary_and_oversized(tmp_path: Path) -> None:
    ws = Workspace.at(tmp_path / "ws", max_file_bytes=5)
    (ws.root / "bin.dat").write_bytes(b"a\x00b")
    with pytest.raises(ValueError, match="Binary"):
        ws.read_file("bin.dat")
    with pytest.raises(ValueError, match="limit"):
        ws.write_file("big.txt", "123456")


def test_excludes_state_directories(tmp_path: Path) -> None:
    ws = Workspace.at(tmp_path / "ws")
    ws.write_file(".mla/state.txt", "x")
    ws.write_file("keep.txt", "x")
    assert ws.list_files() == ["keep.txt"]
