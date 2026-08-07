from pathlib import Path

import pytest

from minimal_local_agent.security import WorkspaceGuard
from minimal_local_agent.workspace import WorkspaceTools


def _tools(tmp_path: Path) -> WorkspaceTools:
    return WorkspaceTools(WorkspaceGuard(tmp_path / "workspace"))


def test_write_read_list_and_search(tmp_path: Path) -> None:
    tools = _tools(tmp_path)

    written = tools.write_file("notes/hello.txt", "Hello\nLocal Agent\n")

    assert written == 18
    assert tools.read_file("notes/hello.txt") == "Hello\nLocal Agent\n"
    assert "notes/hello.txt" in tools.list_files()
    assert tools.search_text("local agent")[0] == {
        "path": "notes/hello.txt",
        "line": 2,
        "text": "Local Agent",
    }


def test_refuses_implicit_overwrite(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.write_file("note.txt", "first")

    with pytest.raises(FileExistsError, match="overwrite=true"):
        tools.write_file("note.txt", "second")

    tools.write_file("note.txt", "second", overwrite=True)
    assert tools.read_file("note.txt") == "second"


def test_rejects_binary_and_oversized_files(tmp_path: Path) -> None:
    tools = WorkspaceTools(WorkspaceGuard(tmp_path / "workspace"), max_file_bytes=5)
    binary = tools.guard.root / "binary.dat"
    binary.write_bytes(b"a\x00b")

    with pytest.raises(ValueError, match="Binary"):
        tools.read_file("binary.dat")
    with pytest.raises(ValueError, match="limit"):
        tools.write_file("large.txt", "123456")
