import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from minimal_local_agent.memory import MemoryConflictError, SQLiteMemoryStore
from minimal_local_agent.store import AuditStore


@pytest.fixture
def memory(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(tmp_path / "memory.db")


def test_candidate_approval_is_explicit_and_persists(tmp_path: Path) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.db")
    candidate = memory.propose("project", "Prefer concise answers", source="user:42")
    assert candidate.status == "candidate"
    assert memory.search("project", "concise") == []
    approved = memory.approve("project", candidate.id, expected_revision=1)
    assert approved.status == "approved"
    assert approved.revision == 2
    reopened = SQLiteMemoryStore(tmp_path / "memory.db")
    assert reopened.search("project", "concise")[0].to_dict() == approved.to_dict()
    assert approved.to_dict()["source"] == "user:42"


def test_rejected_and_forgotten_records_are_not_retrieved(
    memory: SQLiteMemoryStore,
) -> None:
    rejected = memory.propose("project", "An unconfirmed detail")
    memory.reject("project", rejected.id, expected_revision=1)
    candidate = memory.propose("project", "A confirmed detail")
    approved = memory.approve("project", candidate.id, expected_revision=1)
    forgotten = memory.forget("project", approved.id, expected_revision=2)
    assert forgotten.status == "forgotten"
    assert memory.search("project", "detail") == []
    # Excluding retrieval does not pretend to erase archived knowledge/history.
    assert memory.get("project", approved.id).content == "A confirmed detail"
    assert len(memory.list_records("project", status="rejected")) == 1
    assert len(memory.list_records("project", status="forgotten")) == 1


def test_scope_isolation_for_reads_and_modifications(memory: SQLiteMemoryStore) -> None:
    candidate = memory.propose("one", "private detail", entity_key="project")
    approved = memory.approve("one", candidate.id, expected_revision=1)
    assert memory.get("two", approved.id) is None
    assert memory.search("two", "private") == []
    assert memory.list_records("two") == []
    for operation in (memory.approve, memory.reject, memory.forget):
        with pytest.raises(ValueError, match="not found"):
            operation("two", approved.id, expected_revision=2)
    other = memory.propose("two", "another detail", entity_key="project")
    assert memory.approve("two", other.id, expected_revision=1).status == "approved"


def test_entity_candidate_cannot_overwrite_approved_record(
    memory: SQLiteMemoryStore,
) -> None:
    original = memory.propose(
        "project",
        "Local agent",
        entity_key="mla",
        attributes={"language": "Python"},
    )
    approved = memory.approve("project", original.id, expected_revision=1)
    conflicting = memory.propose(
        "project",
        "New claim",
        entity_key="mla",
        attributes={"language": "Rust"},
    )
    assert memory.search("project", "Python")[0].id == approved.id
    with pytest.raises(MemoryConflictError, match="propose_update"):
        memory.approve("project", conflicting.id, expected_revision=1)
    assert memory.get("project", conflicting.id).status == "candidate"
    assert memory.get("project", approved.id).attributes == {"language": "Python"}


def test_entity_update_keeps_old_revision_until_approval(
    memory: SQLiteMemoryStore,
) -> None:
    original = memory.propose(
        "project",
        "Local agent",
        source="notes:1",
        entity_key="mla",
        attributes={"version": "1.0"},
    )
    approved = memory.approve("project", original.id, expected_revision=1)
    update = memory.propose_update(
        "project",
        approved.id,
        content="Local agent 1.1",
        attributes={"version": "1.1"},
        expected_revision=2,
    )
    assert update.base_revision == 2
    assert update.replaces_id == approved.id
    assert memory.search("project", "1.0")[0].id == approved.id
    assert memory.search("project", "1.1") == []
    revised = memory.approve("project", update.id, expected_revision=1)
    assert revised.attributes == {"version": "1.1"}
    assert revised.source == "notes:1"
    assert memory.get("project", approved.id).status == "superseded"
    assert memory.search("project", "1.0") == []
    assert memory.search("project", "1.1")[0].id == revised.id


def test_stale_update_does_not_replace_newer_entity(memory: SQLiteMemoryStore) -> None:
    original = memory.propose("project", "version one", entity_key="mla")
    approved = memory.approve("project", original.id, expected_revision=1)
    first = memory.propose_update(
        "project",
        approved.id,
        content="version two",
        expected_revision=2,
    )
    stale = memory.propose_update(
        "project",
        approved.id,
        content="version three",
        expected_revision=2,
    )
    memory.approve("project", first.id, expected_revision=1)
    with pytest.raises(MemoryConflictError, match="revision changed"):
        memory.approve("project", stale.id, expected_revision=1)
    assert memory.get("project", stale.id).status == "candidate"
    assert memory.search("project", "two")[0].id == first.id
    assert memory.search("project", "three") == []


def test_reject_update_leaves_approved_fact_intact(memory: SQLiteMemoryStore) -> None:
    original = memory.propose("project", "first fact")
    approved = memory.approve("project", original.id, expected_revision=1)
    update = memory.propose_update(
        "project",
        approved.id,
        content="second fact",
        expected_revision=2,
    )
    memory.reject("project", update.id, expected_revision=1)
    assert memory.search("project", "first")[0].id == approved.id
    with pytest.raises(MemoryConflictError, match="candidate"):
        memory.approve("project", update.id, expected_revision=2)


def test_concurrent_approval_of_same_revision_has_one_winner(
    memory: SQLiteMemoryStore,
) -> None:
    candidate = memory.propose("project", "race condition")

    def approve() -> str:
        try:
            return memory.approve("project", candidate.id, expected_revision=1).status
        except MemoryConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: approve(), range(2)))
    assert sorted(results) == ["approved", "conflict"]


def test_keyword_retrieval_is_bounded_and_literal(memory: SQLiteMemoryStore) -> None:
    for text in ("项目使用 Python", "Straße", "literal %_ marker", "ordinary marker"):
        candidate = memory.propose("project", text)
        memory.approve("project", candidate.id, expected_revision=1)
    assert memory.search("project", "项目")[0].content == "项目使用 Python"
    assert memory.search("project", "STRASSE")[0].content == "Straße"
    assert memory.search("project", "%_")[0].content == "literal %_ marker"
    assert len(memory.search("project", "marker", limit=1)) == 1
    assert len(memory.list_records("project", limit=2)) == 2
    data = memory.list_records("project")[0].to_dict()
    data["attributes"]["extra"] = "not saved"
    assert memory.get("project", data["id"]).attributes == {}


def test_memory_tables_do_not_change_audit_schema(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    audit = AuditStore(database)
    session_id = audit.new_session()
    version = audit.schema_version
    memory = SQLiteMemoryStore(database)
    memory.propose("project", "candidate")
    assert audit.schema_version == version
    assert audit.list_sessions()[0]["id"] == session_id


def test_future_memory_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    SQLiteMemoryStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memory_meta SET value = 2 WHERE key = 'schema_version'"
        )
    with pytest.raises(ValueError, match="schema"):
        SQLiteMemoryStore(path)


@pytest.mark.parametrize("revision", [True, 0, -1, "1", None])
def test_invalid_or_stale_revisions(
    memory: SQLiteMemoryStore, revision: object
) -> None:
    candidate = memory.propose("project", "candidate")
    with pytest.raises(ValueError, match="expected_revision"):
        memory.approve("project", candidate.id, expected_revision=revision)
    with pytest.raises(MemoryConflictError):
        memory.reject("project", candidate.id, expected_revision=2)


@pytest.mark.parametrize("limit", [True, 0, -1, 101, "10"])
def test_search_and_listing_limits(memory: SQLiteMemoryStore, limit: object) -> None:
    with pytest.raises(ValueError, match="limit"):
        memory.search("project", "query", limit=limit)
    with pytest.raises(ValueError, match="limit"):
        memory.list_records("project", limit=limit)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": ""},
        {"scope": "s" * 241},
        {"content": " "},
        {"content": "x" * 8_001},
        {"source": "s" * 2_001},
        {"entity_key": ""},
        {"attributes": {"version": "1"}},
        {"entity_key": "mla", "attributes": {"version": 1}},
        {"entity_key": "mla", "attributes": {"k" * 121: "v"}},
        {"entity_key": "mla", "attributes": {"k": "v" * 2_001}},
        {"entity_key": "mla", "attributes": {str(i): "v" for i in range(33)}},
        {"entity_key": "mla", "attributes": []},
    ],
)
def test_invalid_candidates_do_not_write(
    memory: SQLiteMemoryStore, kwargs: dict
) -> None:
    arguments = {"scope": "project", "content": "candidate", **kwargs}
    with pytest.raises(ValueError):
        memory.propose(**arguments)
    assert memory.list_records("project") == []


def test_capacity_and_other_input_limits(tmp_path: Path) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.db", max_records=1)
    first = memory.propose("project", "first")
    memory.forget("project", first.id, expected_revision=1)
    with pytest.raises(ValueError, match="capacity"):
        memory.propose("project", "second")
    for query in ("", "x" * 2_001, " ".join(str(i) for i in range(17))):
        with pytest.raises(ValueError):
            memory.search("project", query)
    with pytest.raises(ValueError, match="status"):
        memory.list_records("project", status="invalid")
    with pytest.raises(ValueError):
        SQLiteMemoryStore(tmp_path / "bad.db", max_records=True)
    with pytest.raises(ValueError):
        SQLiteMemoryStore(":memory:")
    with pytest.raises(ValueError, match="path"):
        SQLiteMemoryStore("")
