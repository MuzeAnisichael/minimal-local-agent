from pathlib import Path

import pytest

from minimal_local_agent.mutations import EditRequest, MutationEngine, sha256_text
from minimal_local_agent.security import WorkspaceGuard
from minimal_local_agent.store import AuditStore
from minimal_local_agent.workspace import WorkspaceTools


def _engine(tmp_path: Path) -> tuple[MutationEngine, AuditStore, WorkspaceTools, str]:
    store = AuditStore(tmp_path / "state.db")
    workspace = WorkspaceTools(WorkspaceGuard(tmp_path / "workspace"))
    session_id = store.new_session()
    return MutationEngine(workspace, store), store, workspace, session_id


def test_commits_multi_file_edit_and_undoes_it(tmp_path: Path) -> None:
    engine, store, workspace, session_id = _engine(tmp_path)
    workspace.write_file("a.txt", "alpha one\n")
    workspace.write_file("b.txt", "beta one\n")

    transaction = engine.prepare_edits(
        [
            EditRequest(path="a.txt", old_text="one", new_text="two"),
            EditRequest(
                path="b.txt",
                old_text="one",
                new_text="three",
                expected_sha256=sha256_text("beta one\n"),
            ),
        ]
    )
    change_set_id = engine.commit(transaction, session_id=session_id, kind="edit_files")

    assert workspace.read_file("a.txt") == "alpha two\n"
    assert workspace.read_file("b.txt") == "beta three\n"
    assert store.get_change_set(change_set_id)["status"] == "applied"

    undo = engine.prepare_undo(change_set_id)
    engine.undo(undo)

    assert workspace.read_file("a.txt") == "alpha one\n"
    assert workspace.read_file("b.txt") == "beta one\n"
    assert store.get_change_set(change_set_id)["status"] == "undone"


def test_undo_removes_file_created_by_change_set(tmp_path: Path) -> None:
    engine, _store, workspace, session_id = _engine(tmp_path)
    transaction = engine.prepare_write("created.txt", "new\n")
    change_set_id = engine.commit(transaction, session_id=session_id, kind="write_file")

    engine.undo(engine.prepare_undo(change_set_id))

    assert not (workspace.guard.root / "created.txt").exists()


def test_rejects_stale_or_ambiguous_edits(tmp_path: Path) -> None:
    engine, _store, workspace, session_id = _engine(tmp_path)
    workspace.write_file("note.txt", "same same\n")

    with pytest.raises(ValueError, match="exactly once"):
        engine.prepare_edits(
            [EditRequest(path="note.txt", old_text="same", new_text="changed")]
        )

    transaction = engine.prepare_edits(
        [EditRequest(path="note.txt", old_text="same same", new_text="ready")]
    )
    workspace.write_file("note.txt", "external\n", overwrite=True)

    with pytest.raises(RuntimeError, match="changed after preview"):
        engine.commit(transaction, session_id=session_id, kind="edit_files")
    assert workspace.read_file("note.txt") == "external\n"


def test_refuses_duplicate_paths_and_incomplete_diffs(tmp_path: Path) -> None:
    engine, _store, workspace, _session_id = _engine(tmp_path)
    workspace.write_file("note.txt", "abc\n")

    with pytest.raises(ValueError, match="same file twice"):
        engine.prepare_edits(
            [
                EditRequest(path="note.txt", old_text="abc", new_text="one"),
                EditRequest(path="note.txt", old_text="abc", new_text="two"),
            ]
        )

    bounded = MutationEngine(workspace, engine.store, max_diff_chars=10)
    with pytest.raises(ValueError, match="approval limit|truncated"):
        bounded.prepare_write("large.txt", "a long new file\n")


def test_rejects_no_op_mutations(tmp_path: Path) -> None:
    engine, _store, workspace, _session_id = _engine(tmp_path)
    workspace.write_file("note.txt", "unchanged\n")

    with pytest.raises(ValueError, match="does not change"):
        engine.prepare_write("note.txt", "unchanged\n", overwrite=True)
    with pytest.raises(ValueError, match="does not change"):
        engine.prepare_edits(
            [
                EditRequest(
                    path="note.txt",
                    old_text="unchanged",
                    new_text="unchanged",
                )
            ]
        )
