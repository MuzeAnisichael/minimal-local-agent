"""Cross-module behavior with deterministic models, never paid model calls."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from minimal_local_agent.agent import AgentRuntime
from minimal_local_agent.config import Settings
from minimal_local_agent.memory import MemoryRecord, SQLiteMemoryStore
from minimal_local_agent.peers import LocalPeer, PeerResult
from minimal_local_agent.planning import Plan, PlanStep


class _Factory:
    def __init__(self, *responses: list) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, _settings: Settings) -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            index = len(self.calls)
            self.calls.append(
                {
                    "messages": ModelMessagesTypeAdapter.dump_python(
                        messages, mode="json"
                    ),
                    "tools": {tool.name for tool in info.function_tools},
                    "settings": dict(info.model_settings or {}),
                }
            )
            parts = (
                self.responses[index]
                if index < len(self.responses)
                else [TextPart("done")]
            )
            return ModelResponse(
                parts, usage=RequestUsage(input_tokens=5, output_tokens=2)
            )

        return FunctionModel(respond)


class _Planner:
    def __init__(self, *, failing: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failing = failing

    def plan(self, prompt: str, **kwargs: Any) -> Plan:
        self.calls.append({"prompt": prompt, **kwargs})
        if self.failing:
            raise ValueError("synthetic planner failure")
        return Plan(
            "plan-advice",
            (PlanStep("ADVISORY_STEP_MARKER", "File content confirmed by a tool"),),
            {"requests": 1, "input_tokens": 7, "output_tokens": 11},
        )


class _Peer:
    def __init__(self, result: PeerResult | None = None) -> None:
        self.result = result or PeerResult(
            "bounded child answer",
            "child-task",
            "completed",
            {"requests": 1, "tool_calls": 0, "input_tokens": 5, "output_tokens": 7},
        )
        self.calls: list[dict[str, Any]] = []

    def call(self, prompt: str, **kwargs: Any) -> PeerResult:
        self.calls.append({"prompt": prompt, **kwargs})
        return self.result


def _settings(tmp_path: Path, **kwargs: Any) -> Settings:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return Settings(
        workspace=workspace,
        database=tmp_path / "state.db",
        write_policy="deny",
        memory_scope="test-project",
        **kwargs,
    )


def _approved(memory: SQLiteMemoryStore, content: str) -> MemoryRecord:
    candidate = memory.propose("test-project", content, source="synthetic fixture")
    return memory.approve("test-project", candidate.id, expected_revision=1)


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _difference(left: Any, right: Any, path: str = "messages") -> str:
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(left.keys() | right.keys()):
            if left.get(key) != right.get(key):
                return _difference(left.get(key), right.get(key), f"{path}.{key}")
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return f"{path}: lengths {len(left)} != {len(right)}"
        for index, (first, second) in enumerate(zip(left, right, strict=True)):
            if first != second:
                return _difference(first, second, f"{path}[{index}]")
    return f"{path}: {left!r} != {right!r}"


def _tool(name: str, arguments: dict[str, Any], call_id: str = "call-one") -> list:
    return [ToolCallPart(name, arguments, tool_call_id=call_id)]


def _failed_receipt(runtime: AgentRuntime) -> dict[str, Any]:
    session_id = runtime.store.list_sessions()[0]["id"]
    assert runtime.store.get_runs(session_id)[-1]["success"] == 0
    receipt = runtime.store.get_receipts(session_id)[-1]["receipt"]
    assert receipt["success"] is False
    assert runtime.store.verify_receipt_chain(session_id)[0]
    return receipt


def test_optional_modules_off_preserve_default_tools_events_and_history(
    tmp_path: Path,
) -> None:
    configured = _settings(tmp_path, memory_database=tmp_path / "unused-memory.db")
    factory = _Factory()
    runtime = AgentRuntime(configured, model_factory=factory)
    events = []
    outcome = runtime.run("basic task", event_handler=events.append)

    assert runtime.memory_store is runtime.planner is None
    assert runtime.peers == {}
    assert not configured.memory_database.exists()
    assert factory.calls[0]["tools"] == {"list_files", "read_file", "search_text"}
    assert [event.type for event in events] == ["run.started", "run.completed"]
    assert "basic task" in runtime.store.load_history(outcome.session_id)
    assert "main_usage" not in outcome.usage


def test_approved_memory_crosses_sessions_but_candidates_and_references_do_not(
    tmp_path: Path,
) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.db")
    _approved(memory, "project preference APPROVED_MEMORY_MARKER")
    memory.propose("test-project", "project preference CANDIDATE_MEMORY_MARKER")
    factory = _Factory()
    runtime = AgentRuntime(
        _settings(tmp_path), model_factory=factory, memory_store=memory
    )

    first = runtime.run("Explain project preference")
    second = runtime.run("Explain project preference")

    assert first.session_id != second.session_id
    for call in factory.calls:
        assert _text(call["messages"]).count("APPROVED_MEMORY_MARKER") == 1
        assert "CANDIDATE_MEMORY_MARKER" not in _text(call["messages"])
    for session_id in (first.session_id, second.session_id):
        saved = runtime.store.load_history(session_id)
        assert "APPROVED_MEMORY_MARKER" not in saved
        assert "approved_memory" not in saved
        assert "Explain project preference" in saved


@pytest.mark.parametrize("foreign", [True, False])
def test_memory_adapter_cannot_inject_foreign_or_unapproved_records(
    tmp_path: Path,
    foreign: bool,
) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.db")
    record = store.propose("elsewhere" if foreign else "test-project", "project memory")
    if foreign:
        record = store.approve(record.scope, record.id, expected_revision=1)

    class InvalidAdapter:
        def search(self, *_args: Any, **_kwargs: Any) -> list[MemoryRecord]:
            return [record]

    factory = _Factory()
    runtime = AgentRuntime(
        _settings(tmp_path),
        model_factory=factory,
        memory_store=InvalidAdapter(),
    )
    with pytest.raises(ValueError, match="unapproved or foreign"):
        runtime.run("project memory")
    assert factory.calls == []
    assert _failed_receipt(runtime)["context_checks"] == []


def test_entity_proposal_tool_never_approves_or_replaces_confirmed_memory(
    tmp_path: Path,
) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.db")
    candidate = memory.propose(
        "test-project",
        "project OLD_ENTITY_MARKER",
        entity_key="mla",
        attributes={"version": "1.0"},
    )
    approved = memory.approve("test-project", candidate.id, expected_revision=1)
    factory = _Factory(
        _tool(
            "memory_propose",
            {
                "content": "project NEW_ENTITY_MARKER",
                "attributes": {"version": "2.0"},
                "replaces_id": approved.id,
                "expected_revision": approved.revision,
            },
        )
    )
    runtime = AgentRuntime(
        _settings(tmp_path), model_factory=factory, memory_store=memory
    )
    outcome = runtime.run("Consider project details")

    proposed = memory.list_records("test-project", status="candidate")
    assert len(proposed) == 1
    assert proposed[0].replaces_id == approved.id
    assert proposed[0].attributes == {"version": "2.0"}
    assert memory.get("test-project", approved.id).status == "approved"
    assert memory.search("test-project", "OLD_ENTITY_MARKER")[0].id == approved.id
    assert memory.search("test-project", "NEW_ENTITY_MARKER") == []
    visible = factory.calls[0]["tools"]
    assert {"memory_search", "memory_propose"} <= visible
    assert not visible & {
        "memory_approve",
        "memory_reject",
        "memory_forget",
        "delete_file",
        "shell",
    }
    event = runtime.store.get_tool_events(outcome.session_id)[0]
    assert event["tool_name"] == "memory_propose" and event["status"] == "candidate"


def test_memory_is_injected_once_through_multiple_tool_loops_and_pairs_survive(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    (settings.workspace / "notes.txt").write_text("seed notes", encoding="utf-8")
    memory = SQLiteMemoryStore(tmp_path / "memory.db")
    _approved(memory, "project STATIC_REFERENCE_MARKER")
    factory = _Factory(
        _tool("read_file", {"path": "notes.txt"}),
        _tool("search_text", {"query": "seed"}, "call-two"),
    )
    runtime = AgentRuntime(settings, model_factory=factory, memory_store=memory)
    outcome = runtime.run("Inspect project notes")

    assert len(factory.calls) == 3
    for call in factory.calls:
        assert _text(call["messages"]).count("STATIC_REFERENCE_MARKER") == 1
        parts = [part for message in call["messages"] for part in message["parts"]]
        called = {p["tool_call_id"] for p in parts if p["part_kind"] == "tool-call"}
        returned = {p["tool_call_id"] for p in parts if p["part_kind"] == "tool-return"}
        assert called == returned
    history = runtime.store.load_history(outcome.session_id)
    assert "STATIC_REFERENCE_MARKER" not in history
    assert "seed notes" in history
    assert "call-one" in history and "call-two" in history


def test_context_reducer_still_preserves_original_history_with_memory(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first = AgentRuntime(settings, model_factory=_Factory()).run("OLD_HISTORY" * 500)
    memory = SQLiteMemoryStore(tmp_path / "memory.db")
    _approved(memory, "project REDUCER_REFERENCE_MARKER")
    factory = _Factory()
    reduced = AgentRuntime(
        replace(settings, max_context_bytes=3_500),
        model_factory=factory,
        memory_store=memory,
        context_reducer=lambda messages, _maximum: messages[-1:],
    )
    second = reduced.run("project next turn", session_id=first.session_id)

    saved = reduced.store.load_history(second.session_id)
    assert "OLD_HISTORY" * 500 in saved
    assert "project next turn" in saved
    assert "REDUCER_REFERENCE_MARKER" not in saved
    receipt = reduced.store.get_receipts(second.session_id)[-1]["receipt"]
    assert receipt["context_checks"][0]["reduced"] is True
    assert receipt["context_checks"][0]["submitted_bytes"] <= 3_500


def test_observation_links_raw_messages_context_model_and_tools_to_execution(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    (settings.workspace / "notes.txt").write_text("observed text", encoding="utf-8")
    memory = SQLiteMemoryStore(tmp_path / "memory.db")
    _approved(memory, "project OBSERVED_REFERENCE_MARKER")
    factory = _Factory(_tool("read_file", {"path": "notes.txt"}))
    runtime = AgentRuntime(settings, model_factory=factory, memory_store=memory)
    events = []
    outcome = runtime.run(
        "Read project notes",
        observe=True,
        execution_id="synthetic-execution",
        event_handler=events.append,
    )

    assert outcome.execution_id == "synthetic-execution"
    assert all(e.data["execution_id"] == outcome.execution_id for e in events)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    types = [e.type for e in events]
    assert types.index("tool.started") < types.index("tool.completed")
    prepared = [e.data["messages"] for e in events if e.type == "context.prepared"]
    started = [e.data["messages"] for e in events if e.type == "model.started"]
    actual = [c["messages"] for c in factory.calls]
    assert started == actual, _difference(started, actual)
    # The SDK may merge adjacent requests before transport. No part may vanish.
    assert len(prepared) == len(actual)
    for selected, transported in zip(prepared, actual, strict=True):
        selected_parts = [part for message in selected for part in message["parts"]]
        actual_parts = [part for message in transported for part in message["parts"]]
        assert selected_parts == actual_parts, _difference(selected_parts, actual_parts)
    assert len([e for e in events if e.type == "model.completed"]) == 2
    original = [e for e in events if e.type == "message.observed"]
    assert all(e.data["scope"] == "original" for e in original)
    assert all("OBSERVED_REFERENCE_MARKER" not in _text(e.data) for e in original)
    assert "observed text" in _text(original[-1].data)
    receipt = runtime.store.get_receipts(outcome.session_id)[-1]["receipt"]
    assert receipt["execution_id"] == outcome.execution_id


def test_planner_injects_advice_not_execution_and_combines_actual_usage(
    tmp_path: Path,
) -> None:
    planner, factory = _Planner(), _Factory()
    events = []
    runtime = AgentRuntime(_settings(tmp_path), model_factory=factory, planner=planner)
    outcome = runtime.run("Plan the task", event_handler=events.append)

    assert len(planner.calls) == len(factory.calls) == 1
    assert "ADVISORY_STEP_MARKER" in _text(factory.calls[0]["messages"])
    assert runtime.store.get_tool_events(outcome.session_id) == []
    assert "ADVISORY_STEP_MARKER" not in runtime.store.load_history(outcome.session_id)
    assert outcome.usage["requests"] == 2
    assert outcome.usage["output_tokens"] == 13
    assert outcome.usage["input_tokens"] == 12
    assert outcome.usage["planning_usage"]["output_tokens"] == 11
    assert (
        next(e for e in events if e.type == "plan.generated").data["state"]
        == "advisory"
    )
    assert factory.calls[0]["settings"]["max_tokens"] == 2_048 - 11


def test_planning_failure_is_audited_before_executor_model_call(tmp_path: Path) -> None:
    planner, factory = _Planner(failing=True), _Factory()
    runtime = AgentRuntime(_settings(tmp_path), model_factory=factory, planner=planner)
    events = []
    with pytest.raises(ValueError, match="synthetic planner failure"):
        runtime.run("Plan task", event_handler=events.append)
    assert factory.calls == []
    assert "plan.failed" in [event.type for event in events]
    assert "plan.generated" not in [event.type for event in events]
    _failed_receipt(runtime)


def test_planning_request_is_deducted_before_another_executor_round(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, request_limit=2)
    (settings.workspace / "notes.txt").write_text("read once", encoding="utf-8")
    factory = _Factory(_tool("read_file", {"path": "notes.txt"}))
    runtime = AgentRuntime(settings, model_factory=factory, planner=_Planner())
    with pytest.raises(ValueError, match="Shared model request budget exhausted"):
        runtime.run("Plan and read notes")
    assert len(factory.calls) == 1
    _failed_receipt(runtime)


@pytest.mark.parametrize("budget", [{"request_limit": 1}, {"max_output_tokens": 1}])
def test_planning_rejects_insufficient_total_budget_before_any_model(
    tmp_path: Path,
    budget: dict[str, int],
) -> None:
    planner, factory = _Planner(), _Factory()
    runtime = AgentRuntime(
        _settings(tmp_path, **budget),
        model_factory=factory,
        planner=planner,
    )
    with pytest.raises(ValueError, match="Planning requires budget"):
        runtime.run("Plan task")
    assert planner.calls == factory.calls == []
    _failed_receipt(runtime)


@pytest.mark.parametrize("same", ["workspace", "database"])
def test_local_peer_cannot_reuse_parent_workspace_or_database(
    tmp_path: Path,
    same: str,
) -> None:
    settings = _settings(tmp_path)
    child = replace(
        settings,
        workspace=tmp_path / "child-workspace",
        database=tmp_path / "child.db",
    )
    child = replace(child, **{same: getattr(settings, same)})
    with pytest.raises(ValueError, match="separate workspace and database"):
        AgentRuntime(
            settings, model_factory=_Factory(), peers={"child": LocalPeer(child)}
        )


def test_known_peer_receives_only_explicit_task_and_bounded_shared_allocation(
    tmp_path: Path,
) -> None:
    peer = _Peer()
    factory = _Factory(
        _tool("delegate_task", {"agent_name": "worker", "task": "isolated task"})
    )
    runtime = AgentRuntime(
        _settings(tmp_path), model_factory=factory, peers={"worker": peer}
    )
    outcome = runtime.run("Parent private task marker")

    assert len(peer.calls) == 1
    assert peer.calls[0]["prompt"] == "isolated task"
    assert peer.calls[0]["request_limit"] == peer.calls[0]["tool_calls_limit"] == 1
    assert 1 <= peer.calls[0]["max_output_tokens"] <= 512
    assert peer.calls[0]["timeout"] == 30.0
    assert "parent" not in _text(peer.calls).lower()
    assert outcome.usage["requests"] == 3
    assert outcome.usage["tool_calls"] == 1
    assert outcome.usage["peer_usage_complete"] is True


def test_unknown_peer_is_refused_without_invoking_configured_peer(
    tmp_path: Path,
) -> None:
    peer = _Peer()
    factory = _Factory(
        _tool("delegate_task", {"agent_name": "unknown", "task": "task"})
    )
    runtime = AgentRuntime(
        _settings(tmp_path), model_factory=factory, peers={"worker": peer}
    )
    with pytest.raises(ValueError, match="Unknown peer"):
        runtime.run("Delegate task")
    assert peer.calls == []
    _failed_receipt(runtime)


def test_pending_peer_is_not_completed_and_unknown_usage_stays_reserved(
    tmp_path: Path,
) -> None:
    peer = _Peer(PeerResult("still working", "child-task", "working"))
    factory = _Factory(
        _tool("delegate_task", {"agent_name": "worker", "task": "task"}),
        [TextPart("The child is still working.")],
    )
    events = []
    runtime = AgentRuntime(
        _settings(tmp_path, request_limit=3),
        model_factory=factory,
        peers={"worker": peer},
    )
    outcome = runtime.run("Delegate task", event_handler=events.append)

    types = [event.type for event in events]
    assert "peer.pending" in types and "peer.completed" not in types
    assert outcome.usage["peer_usage"] == [{}]
    assert outcome.usage["peer_usage_complete"] is False
    assert outcome.usage["reserved_extension_requests"] == 1
    assert outcome.usage["reserved_extension_output_tokens"] > 0
    pending = next(event for event in events if event.type == "peer.pending")
    assert pending.data["state"] == "working"
    assert pending.data["usage_known"] is False
    assert runtime.store.get_tool_events(outcome.session_id)[0]["status"] == "working"


@pytest.mark.parametrize("violation", ["requests", "tokens", "response"])
def test_peer_budget_and_result_overflow_fail_the_run(
    tmp_path: Path, violation: str
) -> None:
    usage = {"requests": 1, "tool_calls": 0, "output_tokens": 7}
    response = "bounded answer"
    if violation == "requests":
        usage["requests"] = 2
    elif violation == "tokens":
        usage["output_tokens"] = 1_024
    else:
        response = "x" * 4_000
    peer = _Peer(PeerResult(response, "child-task", "completed", usage))
    factory = _Factory(_tool("delegate_task", {"agent_name": "worker", "task": "task"}))
    runtime = AgentRuntime(
        _settings(tmp_path, max_context_bytes=3_000),
        model_factory=factory,
        peers={"worker": peer},
    )
    with pytest.raises(ValueError, match="budget"):
        runtime.run("Delegate task")
    assert len(peer.calls) == len(factory.calls) == 1
    _failed_receipt(runtime)


def test_delegation_is_refused_before_peer_when_no_shared_request_remains(
    tmp_path: Path,
) -> None:
    peer = _Peer()
    factory = _Factory(_tool("delegate_task", {"agent_name": "worker", "task": "task"}))
    runtime = AgentRuntime(
        _settings(tmp_path, request_limit=2),
        model_factory=factory,
        peers={"worker": peer},
    )
    with pytest.raises(ValueError, match="No shared request budget"):
        runtime.run("Delegate task")
    assert peer.calls == []
    _failed_receipt(runtime)


def test_provisional_peer_usage_does_not_refund_pending_budget(tmp_path):
    peer = _Peer(
        PeerResult(
            "still working",
            "child-task",
            "working",
            {
                "requests": 0,
                "tool_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        )
    )
    factory = _Factory(_tool("delegate_task", {"agent_name": "worker", "task": "task"}))
    runtime = AgentRuntime(
        _settings(tmp_path, request_limit=3),
        model_factory=factory,
        peers={"worker": peer},
    )
    events = []
    outcome = runtime.run("Delegate task", event_handler=events.append)
    assert outcome.usage["requests"] is None
    assert outcome.usage["reserved_extension_requests"] == 1
    assert outcome.usage["reserved_extension_tool_calls"] == 1
    pending = next(event for event in events if event.type == "peer.pending")
    assert pending.data["usage_known"] is False


def test_final_model_response_over_shared_budget_records_failure(tmp_path):
    class OverBudgetFactory:
        def create(self, _settings):
            return FunctionModel(
                lambda _messages, _info: ModelResponse(
                    [TextPart("provider ignored max_tokens")],
                    usage=RequestUsage(input_tokens=5, output_tokens=95),
                )
            )

    runtime = AgentRuntime(
        _settings(tmp_path, max_output_tokens=100),
        model_factory=OverBudgetFactory(),
        planner=_Planner(),
    )
    with pytest.raises(ValueError, match="Shared output token budget"):
        runtime.run("Plan then answer")
    _failed_receipt(runtime)
