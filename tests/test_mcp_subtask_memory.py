"""M4：MCP 接入、受控子任务、记忆策略。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from pydantic_ai.models.wrapper import WrapperModel  # noqa: E402

from minimal_local_agent.eval import faults  # noqa: E402
from minimal_local_agent.model.scripted import scripted_model  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "notes_server.py"


def _mcp_section(tmp_path: Path, idempotent: bool = False) -> dict:
    return {
        "servers": [
            {
                "name": "notes",
                "command": [sys.executable, str(FIXTURE), str(tmp_path / "notes.json")],
                "tools": {"add_note": "write", "search_notes": "read"},
                "idempotent": ["add_note"] if idempotent else [],
            }
        ]
    }


def test_mcp_tools_go_through_the_ledger(runtime_factory, tmp_path) -> None:
    script = {
        "turns": [
            {"tool": "mcp_notes_add_note", "args": {"text": "UTF-8 everywhere"}},
            {"tool": "mcp_notes_search_notes", "args": {"query": "utf"}},
            {"text": "noted"},
        ]
    }
    rt = runtime_factory(script, mcp=_mcp_section(tmp_path))
    result = rt.run(
        "remember utf-8", checks=[{"type": "answer_contains", "text": "noted"}]
    )
    assert result.ok
    assert json.loads((tmp_path / "notes.json").read_text()) == ["UTF-8 everywhere"]
    rows = {r["tool"]: r for r in rt.store.ledger_for_task(result.task_id)}
    assert rows["mcp_notes_add_note"]["status"] == "done"
    assert "UTF-8" in json.dumps(rows["mcp_notes_search_notes"]["result"])


@pytest.mark.parametrize("idempotent", [False, True])
def test_interrupted_mcp_write_replays_only_when_declared_idempotent(
    runtime_factory, tmp_path, idempotent
) -> None:
    script = {
        "turns": [{"tool": "mcp_notes_add_note", "args": {"text": "n"}}, {"text": "ok"}]
    }
    rt = runtime_factory(script, mcp=_mcp_section(tmp_path, idempotent))
    rt._sync(rt._ensure_mcp())  # 先连接，才能给 MCP 的 effect 注入故障
    faults.crash_during_write(rt, kind="mcp:notes")
    with pytest.raises(faults.SimulatedCrash):
        rt.run("x")
    task_id = rt.store.tasks(1)[0]["id"]

    fresh = runtime_factory(script, mcp=_mcp_section(tmp_path, idempotent))
    fresh.resume(task_id)
    row = fresh.store.ledger_for_task(task_id)[0]
    notes = json.loads((tmp_path / "notes.json").read_text())
    if idempotent:
        assert row["status"] == "done" and len(notes) == 2  # 声明幂等，重放
    else:
        assert row["status"] == "failed" and "OutcomeUnknown" in row["result"]["error"]
        assert len(notes) == 1


class _Router(WrapperModel):
    """测试用：带工具但不含 run_subtask 的请求来自子任务，交给 child 脚本，其余交给 parent 脚本。"""

    def __init__(self, child, parent) -> None:
        super().__init__(parent)
        self.child = child

    async def request(self, messages, settings, params):
        names = [t.name for t in params.function_tools]
        is_child = bool(names) and "run_subtask" not in names
        target = self.child if is_child else self.wrapped
        return await target.request(messages, settings, params)


def test_subtask_has_own_budget_and_restricted_tools(runtime_factory) -> None:
    # 父子任务各用一份脚本，避免共用一个脚本模型时读错位置
    parent = scripted_model(
        {
            "turns": [
                {
                    "tool": "run_subtask",
                    "args": {
                        "goal": "child: read a.txt",
                        "tools": ["read_file"],
                        "max_requests": 3,
                    },
                },
                {"text": "parent done"},
            ]
        }
    )
    child = scripted_model(
        {
            "turns": [
                {"tool": "read_file", "args": {"path": "a.txt"}},
                {"text": "child read it"},
            ]
        }
    )
    rt = runtime_factory({}, models={"default": _Router(child, parent)})
    rt.workspace.write_file("a.txt", "secret\n")
    result = rt.run(
        "parent", checks=[{"type": "answer_contains", "text": "parent done"}]
    )
    assert result.ok
    children = rt.store.tasks(parent_id=result.task_id)
    assert len(children) == 1 and children[0]["status"] == "completed"
    assert children[0]["options"]["tools"] == ["read_file"]
    sub_result = rt.store.ledger_for_task(result.task_id)[0]["result"]["result"]
    assert (
        sub_result["status"] == "completed" and sub_result["answer"] == "child read it"
    )
    assert result.usage["requests"] >= 5  # 子任务用量结算进父账本


def test_subtask_cannot_escalate_tools(runtime_factory) -> None:
    script = {
        "turns": [
            {
                "tool": "run_subtask",
                "args": {"goal": "g", "tools": ["write_file", "run_subtask"]},
            },
            {"text": "ok"},
        ]
    }
    rt = runtime_factory(script, policy={"write": "deny"})
    result = rt.run("x")
    row = rt.store.ledger_for_task(result.task_id)[0]
    assert row["status"] == "failed" and "cannot use tools" in row["result"]["error"]


def test_episodic_memory_recalls_relevant_notes(runtime_factory) -> None:
    rt = runtime_factory(
        {"turns": [{"text": "used utf-8"}]}, memory={"strategy": "episodic"}
    )
    rt.run("convert config.yaml to utf-8")
    rt.run("rename the docs folder")
    result = rt.run("check config.yaml encoding")
    recalled = rt.store.events(result.task_id, kind="memory.recalled")
    assert recalled and "config.yaml" in recalled[0]["data"]["notes"][0]

    plain = runtime_factory({"turns": [{"text": "x"}]})
    plain_result = plain.run("check config.yaml encoding")
    assert not plain.store.events(plain_result.task_id, kind="memory.recalled")
