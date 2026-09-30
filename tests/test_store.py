import sqlite3
from pathlib import Path

import pytest

import minimal_local_agent.store as store_module
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
    assert sessions[0]["last_prompt"] == "hello"
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


@pytest.mark.parametrize("version", [0, 1, 2])
def test_upgrades_legacy_data_without_rewriting_it(
    tmp_path: Path, version: int
) -> None:
    database = tmp_path / "legacy.db"
    fixture = Path(__file__).parent / "fixtures" / "legacy_store.sql"
    with sqlite3.connect(database) as connection:
        connection.executescript(fixture.read_text(encoding="utf-8"))
        if version == 2:
            changes = fixture.with_name("legacy_changes.sql")
            connection.executescript(changes.read_text(encoding="utf-8"))
        connection.execute(f"PRAGMA user_version = {version}")

    store = AuditStore(database)

    assert store.schema_version == 3
    assert store.load_history("legacy") == "[]"
    assert store.get_runs("legacy")[0]["id"] == 42
    assert store.get_runs("legacy")[0]["response"] == "world"
    assert store.get_tool_events("legacy")[0]["id"] == 7
    if version == 2:
        change = store.get_change_set("change-old")
        assert change is not None
        assert change["changes"][0]["before_text"] == "before"
    head = store.record_receipt("legacy", 42, {"schema": "legacy-check"})
    reopened = AuditStore(database)
    assert reopened.verify_receipt_chain("legacy", expected_head=head) == (True, None)


def test_failed_migration_rolls_back_tables_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.db"
    AuditStore(database)
    monkeypatch.setattr(
        store_module,
        "MIGRATIONS",
        (
            *store_module.MIGRATIONS,
            (4, "CREATE TABLE probe (id INTEGER); INVALID SQL;"),
        ),
    )

    with pytest.raises(sqlite3.OperationalError):
        AuditStore(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'probe'"
            ).fetchone()
            is None
        )


def test_refuses_future_schema_without_changing_database(tmp_path: Path) -> None:
    database = tmp_path / "future.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 4")
    original = database.read_bytes()

    with pytest.raises(ValueError, match="newer than supported"):
        AuditStore(database)

    assert database.read_bytes() == original
    assert not database.with_name(database.name + "-wal").exists()
