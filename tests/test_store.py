from pathlib import Path

from minimal_local_agent.store import AuditStore


def test_persists_session_history_and_runs(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "state.db")
    session_id = store.new_session()

    store.save_history(session_id, '[{"kind":"message"}]')
    store.record_run(
        session_id,
        "hello",
        response="world",
        success=True,
        usage={"requests": 1},
    )
    store.record_tool_event(session_id, "read_file", "ok", {"path": "a.txt"})

    assert store.load_history(session_id) == '[{"kind":"message"}]'
    assert store.get_runs(session_id)[0]["response"] == "world"
    event = store.get_tool_events(session_id)[0]
    assert event == {
        "id": 1,
        "created_at": event["created_at"],
        "tool_name": "read_file",
        "status": "ok",
        "details": {"path": "a.txt"},
    }
    sessions = store.list_sessions()
    assert sessions[0]["id"] == session_id
    assert sessions[0]["run_count"] == 1

    moved = tmp_path / "moved.db"
    store.path.replace(moved)
    assert moved.exists()
