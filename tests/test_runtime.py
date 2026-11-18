"""M1：最小循环、统一执行入口、事件轨迹、预算与策略。"""

from __future__ import annotations

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from minimal_local_agent.model.scripted import scripted_model
from minimal_local_agent.runtime import trace

WRITE = {"tool": "write_file", "args": {"path": "a.txt", "content": "hi\n"}}


def test_completes_and_traces_artifact_to_model_call(runtime_factory) -> None:
    rt = runtime_factory({"turns": [WRITE, {"text": "done"}]})
    result = rt.run(
        "create a.txt",
        checks=[{"type": "file_equals", "path": "a.txt", "content": "hi\n"}],
    )

    assert result.ok and result.answer == "done"
    assert (rt.workspace.root / "a.txt").read_text() == "hi\n"
    chain = [e["kind"] for e in trace.artifact(rt.store, path="a.txt")]
    assert chain == [
        "artifact.written",
        "tool.completed",
        "tool.called",
        "model.called",
        "step.started",
        "task.started",
    ]
    root = trace.artifact(rt.store, path="a.txt")[-1]
    assert root["data"]["goal"] == "create a.txt"
    assert result.usage["requests"] == 3  # 规划 + 两个执行步骤
    assert result.usage["cost_usd"] is not None


def test_failed_verification_replans_then_fails_and_compensates(
    runtime_factory,
) -> None:
    rt = runtime_factory(
        {"turns": [WRITE, {"text": "done"}, {"text": "still done"}]},
        planning={"max_replans": 1},
    )
    result = rt.run(
        "x", checks=[{"type": "file_contains", "path": "a.txt", "text": "bye"}]
    )

    assert result.status == "failed"
    assert "VerificationFailed" in (result.error or "")
    assert len(rt.store.events(result.task_id, kind="verify.failed")) == 2
    # 失败后补偿：a.txt 原本不存在，被删除
    assert not (rt.workspace.root / "a.txt").exists()
    assert rt.store.ledger_for_task(result.task_id)[0]["status"] == "compensated"


def test_policy_preview_and_denied_approval_do_not_write(runtime_factory) -> None:
    preview = runtime_factory(
        {"turns": [WRITE, {"text": "ok"}]}, policy={"write": "preview"}
    )
    preview.run("x")
    assert not (preview.workspace.root / "a.txt").exists()

    denied = runtime_factory(
        {"turns": [WRITE, {"text": "ok"}]}, approver=lambda *_: False
    )
    result = denied.run("x")
    returned = denied.store.ledger_for_task(result.task_id)[0]["result"]
    assert returned["code"] == "approval_denied"
    assert not (denied.workspace.root / "a.txt").exists()


def test_denied_tool_is_hidden_and_rejected(runtime_factory) -> None:
    seen: list[list[str]] = []

    def respond(messages, info):
        seen.append([t.name for t in info.function_tools])
        return ModelResponse(parts=[TextPart("ok")])

    rt = runtime_factory(
        {},
        models={"default": FunctionModel(respond)},
        policy={"tools": {"search_text": "deny"}},
    )
    rt.run("x")
    assert "search_text" not in seen[-1] and "read_file" in seen[-1]


def test_invalid_and_unknown_tool_calls_are_reported_to_model(runtime_factory) -> None:
    rt = runtime_factory(
        {
            "turns": [
                {"tool": "nope", "args": {}},
                {"tool": "read_file", "args": "{bad"},
                {"text": "ok"},
            ]
        }
    )
    result = rt.run("x")
    assert result.ok
    rejected = rt.store.events(result.task_id, kind="tool.rejected")
    assert len(rejected) == 2


def test_tool_errors_are_returned_not_raised(runtime_factory) -> None:
    rt = runtime_factory(
        {"turns": [{"tool": "read_file", "args": {"path": "../x"}}, {"text": "ok"}]}
    )
    result = rt.run("x")
    row = rt.store.ledger_for_task(result.task_id)[0]
    assert row["status"] == "failed" and "escapes" in row["result"]["error"]


def test_request_budget_exhaustion_fails_task(runtime_factory) -> None:
    loop = {"turns": [{"tool": "list_files", "args": {}}] * 10}
    rt = runtime_factory(loop, budget={"requests": 3})
    result = rt.run("x")
    assert result.status == "failed" and "budget" in (result.error or "").lower()
    assert result.usage["requests"] == 3


def test_tool_budget_counts_each_call_once(runtime_factory) -> None:
    rt = runtime_factory(
        {
            "turns": [
                {"tools": [{"tool": "list_files", "args": {}}] * 3},
                {"text": "ok"},
            ]
        },
        budget={"tool_calls": 2},
    )
    result = rt.run("x")
    assert result.status == "failed" and result.usage["tool_calls"] == 2


def test_model_fallback_on_failure(tmp_path) -> None:
    from minimal_local_agent.config import ModelConfig
    from minimal_local_agent.runtime.runtime import AgentRuntime
    from tests.conftest import make_settings

    def broken(messages, info):
        raise ConnectionError("down")

    settings = make_settings(
        tmp_path,
        models={
            "default": ModelConfig(provider="test"),
            "backup": ModelConfig(provider="test"),
        },
        routing={
            "plan": ["default", "backup"],
            "act": ["default", "backup"],
            "summarize": ["backup"],
        },
    )
    backup = scripted_model({"turns": [{"text": "from backup"}]})
    rt = AgentRuntime(
        settings, models={"default": FunctionModel(broken), "backup": backup}
    )
    result = rt.run("x")
    assert result.ok and result.answer == "from backup"
    failures = rt.store.events(result.task_id, kind="model.failed")
    assert failures and failures[0]["data"]["model"] == "default"
    assert result.usage["cost_complete"] is False  # 没配价格 → 成本未知


def test_planning_disabled_skips_plan_call(runtime_factory) -> None:
    rt = runtime_factory({"turns": [{"text": "ok"}]}, planning={"enabled": False})
    result = rt.run("x")
    assert result.usage["requests"] == 1
    assert not rt.store.events(result.task_id, kind="plan.created")
