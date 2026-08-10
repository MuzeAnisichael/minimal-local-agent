import sqlite3
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
    assert store.schema_version == 3

    moved = tmp_path / "moved.db"
    store.path.replace(moved)
    assert moved.exists()


def test_receipt_chain_detects_content_tampering(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "state.db")
    session_id = store.new_session()
    first_run = store.record_run(session_id, "one", response="1", success=True)
    first_hash = store.record_receipt(session_id, first_run, {"run": 1})
    second_run = store.record_run(session_id, "two", response="2", success=True)
    second_hash = store.record_receipt(session_id, second_run, {"run": 2})

    receipts = store.get_receipts(session_id)
    assert receipts[0]["receipt_hash"] == first_hash
    assert receipts[1]["previous_hash"] == first_hash
    assert receipts[1]["receipt_hash"] == second_hash
    assert store.verify_receipt_chain(session_id) == (True, None)
    assert store.verify_receipt_chain(session_id, expected_head=second_hash) == (
        True,
        None,
    )
    valid, error = store.verify_receipt_chain(session_id, expected_head="0" * 64)
    assert not valid
    assert error == "receipt chain head does not match the expected hash"

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE execution_receipts SET receipt_json = '{}' WHERE id = 1"
        )

    valid, error = store.verify_receipt_chain(session_id)
    assert not valid
    assert error == "receipt 1 content hash does not match"


def test_empty_receipt_chain_is_not_reported_as_verified(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "state.db")
    session_id = store.new_session()

    assert store.verify_receipt_chain(session_id) == (
        False,
        "session has no execution receipts",
    )
