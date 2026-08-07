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


def test_previews_new_and_changed_files_as_unified_diff(tmp_path: Path) -> None:
    tools = _tools(tmp_path)

    created = tools.preview_write("notes/new.txt", "first\n")

    assert created.path == "notes/new.txt"
    assert created.existed is False
    assert "--- /dev/null" in created.diff
    assert "+++ b/notes/new.txt" in created.diff
    assert "+first" in created.diff

    tools.write_file("notes/new.txt", "first\n")
    changed = tools.preview_write("notes/new.txt", "second\n", overwrite=True)

    assert changed.existed is True
    assert "--- a/notes/new.txt" in changed.diff
    assert "-first" in changed.diff
    assert "+second" in changed.diff


def test_preview_is_bounded_and_validates_before_approval(tmp_path: Path) -> None:
    tools = _tools(tmp_path)

    preview = tools.preview_write("long.txt", "abcdefghij", max_diff_chars=8)

    assert preview.diff_truncated is True
    assert preview.diff.endswith("... [diff truncated]")
    tools.write_file("existing.txt", "first")
    with pytest.raises(FileExistsError, match="overwrite=true"):
        tools.preview_write("existing.txt", "second")


def test_preview_makes_line_ending_changes_visible(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.write_file("ending.txt", "same text\n")

    missing_newline = tools.preview_write("ending.txt", "same text", overwrite=True)
    windows_newline = tools.preview_write("ending.txt", "same text\r\n", overwrite=True)

    assert "no newline at end of file" in missing_newline.diff
    assert "CRLF" in windows_newline.diff


def test_rejects_binary_and_oversized_files(tmp_path: Path) -> None:
    tools = WorkspaceTools(WorkspaceGuard(tmp_path / "workspace"), max_file_bytes=5)
    binary = tools.guard.root / "binary.dat"
    binary.write_bytes(b"a\x00b")

    with pytest.raises(ValueError, match="Binary"):
        tools.read_file("binary.dat")
    with pytest.raises(ValueError, match="limit"):
        tools.write_file("large.txt", "123456")
