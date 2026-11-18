"""M3：规划、校验、按角色路由、上下文 token 预算与压缩。"""

from __future__ import annotations

import sys

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel

from minimal_local_agent.config import ModelConfig
from minimal_local_agent.core import context as ctxmod
from minimal_local_agent.core.plan import parse_plan
from minimal_local_agent.runtime.runtime import AgentRuntime
from tests.conftest import make_settings

PLAN = {"steps": [{"id": "s1", "title": "write", "done_when": "a.txt exists"}]}


def test_plan_is_created_updated_and_survives_resume(runtime_factory) -> None:
    from minimal_local_agent.eval import faults

    script = {
        "plan": PLAN,
        "turns": [
            {"tool": "update_plan", "args": {"step_id": "s1", "status": "done"}},
            {"tool": "write_file", "args": {"path": "a.txt", "content": "x"}},
            {"text": "done"},
        ],
    }
    rt = runtime_factory(script)
    faults.crash_before_tool(rt, "write_file")
    with pytest.raises(faults.SimulatedCrash):
        rt.run("x")
    task_id = rt.store.tasks(1)[0]["id"]
    state = rt.load_state(task_id)
    assert state.plan is not None and state.plan.steps[0].status == "done"

    fresh = runtime_factory(script)
    assert fresh.resume(task_id).ok
    assert "s1" in str(fresh.store.events(task_id, kind="plan.created")[0]["data"])


def test_invalid_plan_is_recorded_and_task_continues(tmp_path) -> None:
    def respond(messages, info):
        if not info.function_tools:
            return ModelResponse(parts=[TextPart("I will just do it")])
        return ModelResponse(parts=[TextPart("ok")])

    settings = make_settings(tmp_path, models={"default": ModelConfig(provider="test")})
    rt = AgentRuntime(settings, models={"default": FunctionModel(respond)})
    result = rt.run("x")
    assert result.ok
    assert rt.store.events(result.task_id, kind="plan.invalid")


def test_parse_plan_extracts_json() -> None:
    plan = parse_plan('Sure:\n{"steps": [{"title": "a"}, {"id": "x", "title": "b"}]}')
    assert [s.id for s in plan.steps] == ["s1", "x"]


def test_roles_route_to_different_models(tmp_path) -> None:
    seen: list[str] = []

    def planner(messages, info):
        seen.append("planner")
        return ModelResponse(parts=[TextPart('{"steps": [{"title": "go"}]}')])

    def actor(messages, info):
        seen.append("actor")
        return ModelResponse(parts=[TextPart("ok")])

    settings = make_settings(
        tmp_path,
        models={
            "big": ModelConfig(provider="test"),
            "small": ModelConfig(provider="test"),
        },
        routing={"plan": ["big"], "act": ["small"], "summarize": ["small"]},
    )
    rt = AgentRuntime(
        settings, models={"big": FunctionModel(planner), "small": FunctionModel(actor)}
    )
    result = rt.run("x")
    assert result.ok and seen == ["planner", "actor"]
    roles = {
        e["data"]["role"]: e["data"]["model"]
        for e in rt.store.events(result.task_id, kind="model.called")
    }
    assert roles == {"plan": "big", "act": "small"}


def test_command_and_answer_checks(runtime_factory) -> None:
    rt = runtime_factory({"turns": [{"text": "the answer is 42"}]})
    ok = rt.run(
        "x",
        checks=[
            {"type": "answer_contains", "text": "42"},
            {"type": "command", "argv": [sys.executable, "-c", "print('ok')"]},
        ],
    )
    assert ok.ok
    bad = rt.run(
        "x",
        checks=[
            {"type": "command", "argv": [sys.executable, "-c", "raise SystemExit(3)"]}
        ],
    )
    assert bad.status == "failed" and "exit 3" in bad.verification[0]["detail"]


def _assert_tool_pairs_intact(messages) -> None:
    assert isinstance(messages[0], ModelRequest)
    assert isinstance(messages[1], ModelResponse)
    open_calls: set[str] = set()
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                open_calls.add(part.tool_call_id)
            if isinstance(part, ToolReturnPart):
                assert part.tool_call_id in open_calls


def test_long_task_is_compacted_by_steps(runtime_factory) -> None:
    turns = [{"tool": "read_file", "args": {"path": "big.txt"}} for _ in range(8)] + [
        {"text": "done"}
    ]
    rt = runtime_factory(
        {"turns": turns},
        context={"max_tokens": 3000, "keep_recent_steps": 2},
        budget={"requests": 20},
    )
    rt.workspace.write_file("big.txt", "word " * 800)
    result = rt.run("read a lot")
    assert result.ok
    compactions = rt.store.events(result.task_id, kind="context.compacted")
    assert compactions and all(c["data"]["after_tokens"] <= 3000 for c in compactions)
    state = rt.load_state(result.task_id)
    _assert_tool_pairs_intact(state.messages)
    head = str(state.messages[0].parts[-1].content)
    assert head.count("[Context compacted:") == 1 and "Current plan:" in head


def test_summarize_mode_uses_summarize_role_and_budget(runtime_factory) -> None:
    turns = [{"tool": "read_file", "args": {"path": "big.txt"}} for _ in range(5)] + [
        {"text": "done"}
    ]
    rt = runtime_factory(
        {"turns": turns},
        context={"max_tokens": 3000, "keep_recent_steps": 1, "compaction": "summarize"},
        budget={"requests": 30},
    )
    rt.workspace.write_file("big.txt", "word " * 800)
    result = rt.run("x")
    assert result.ok
    roles = [
        e["data"]["role"] for e in rt.store.events(result.task_id, kind="model.called")
    ]
    assert "summarize" in roles
    assert result.usage["requests"] == len(roles)


def test_context_overflow_fails_cleanly(runtime_factory) -> None:
    rt = runtime_factory({"turns": [{"text": "x"}]}, context={"max_tokens": 256})
    result = rt.run("y" * 3000)
    assert result.status == "failed" and "ContextOverflow" in (result.error or "")


def test_token_estimate_counts_cjk_per_character() -> None:
    ascii_req = [ModelRequest.user_text_prompt("a" * 400)]
    cjk_req = [ModelRequest.user_text_prompt("中" * 400)]
    assert ctxmod.estimate_tokens(cjk_req) > ctxmod.estimate_tokens(ascii_req) * 2


def test_parse_plan_skips_braces_and_code_fences() -> None:
    text = 'Plan for {goal}:\n```json\n{"steps": [{"title": "read"}]}\n```\nThat is all {ok}.'
    assert [s.title for s in parse_plan(text).steps] == ["read"]
