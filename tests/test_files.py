from pathlib import Path

import pytest

from minimal_local_agent.tools import files
from minimal_local_agent.tools.files import FileChangeSet, FileConflictError
from minimal_local_agent.tools.fs import Workspace


def _ws(tmp_path: Path) -> Workspace:
    return Workspace.at(tmp_path / "ws")


def test_multi_file_edit_apply_and_compensate(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    ws.write_file("a.txt", "alpha one\n")
    ws.write_file("b.txt", "beta one\n")
    cs = files.prepare_edits(
        ws,
        [
            {"path": "a.txt", "old_text": "one", "new_text": "two"},
            {"path": "b.txt", "old_text": "one", "new_text": "three"},
        ],
    )
    assert files.probe(ws, cs) == "before"
    files.apply(ws, cs)
    assert ws.read_file("a.txt") == "alpha two\n"
    assert files.probe(ws, cs) == "after"
    files.compensate(ws, cs)
    assert ws.read_file("b.txt") == "beta one\n"
    assert files.probe(ws, cs) == "before"


def test_change_set_round_trips_through_json(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    cs = files.prepare_write(ws, "new.txt", "hi\n", overwrite=False)
    again = FileChangeSet.from_dict(cs.to_dict())
    files.apply(ws, again)
    assert ws.read_file("new.txt") == "hi\n"
    files.compensate(ws, again)
    assert not (ws.root / "new.txt").exists()
    assert "+hi" in cs.diff()


def test_partial_state_rolls_forward(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    ws.write_file("a.txt", "1\n")
    ws.write_file("b.txt", "1\n")
    cs = files.prepare_edits(
        ws,
        [
            {"path": "a.txt", "old_text": "1", "new_text": "2"},
            {"path": "b.txt", "old_text": "1", "new_text": "2"},
        ],
    )
    ws.write_file("a.txt", "2\n")  # 模拟写到一半崩溃
    assert files.probe(ws, cs) == "partial"
    files.apply(ws, cs)
    assert files.probe(ws, cs) == "after"


def test_external_edit_is_a_conflict(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    ws.write_file("note.txt", "same same\n")
    with pytest.raises(ValueError, match="exactly once"):
        files.prepare_edits(
            ws, [{"path": "note.txt", "old_text": "same", "new_text": "x"}]
        )
    cs = files.prepare_edits(
        ws, [{"path": "note.txt", "old_text": "same same", "new_text": "ready"}]
    )
    ws.write_file("note.txt", "external\n")
    assert files.probe(ws, cs) == "conflict"
    with pytest.raises(FileConflictError):
        files.apply(ws, cs)
    with pytest.raises(FileConflictError):
        files.compensate(ws, cs)
    assert ws.read_file("note.txt") == "external\n"


def test_rejects_noop_duplicate_and_implicit_overwrite(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    ws.write_file("n.txt", "abc\n")
    with pytest.raises(FileExistsError):
        files.prepare_write(ws, "n.txt", "x", overwrite=False)
    with pytest.raises(ValueError, match="does not change"):
        files.prepare_write(ws, "n.txt", "abc\n", overwrite=True)
    with pytest.raises(ValueError, match="twice"):
        files.prepare_edits(
            ws,
            [
                {"path": "n.txt", "old_text": "a", "new_text": "b"},
                {"path": "n.txt", "old_text": "c", "new_text": "d"},
            ],
        )
