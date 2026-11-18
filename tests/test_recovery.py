"""M2：检查点、幂等账本、对账、补偿、取消与超时。"""

from __future__ import annotations

import asyncio
import time

import pytest

from minimal_local_agent.eval import faults
from minimal_local_agent.runtime.store import Store
from minimal_local_agent.tools.registry import Tool, schema

WRITE = {"tool": "write_file", "args": {"path": "a.txt", "content": "hi\n"}}
EQUALS = [{"type": "file_equals", "path": "a.txt", "content": "hi\n"}]


def _crash(rt, goal, checks=None):
    with pytest.raises(faults.SimulatedCrash):
        rt.run(goal, checks=checks)
    task = rt.store.tasks(1)[0]
    assert task["status"] == "running"
    return task["id"]


def counter_tool(calls: list[str]) -> Tool:
    async def run(ctx, args):
        calls.append(args["tag"])
        return args["tag"]

    return Tool(
        "count", "count", schema({"tag": {"type": "string"}}, ["tag"]), "read", run=run
    )


def test_crash_after_write_is_reconciled_not_repeated(runtime_factory) -> None:
    rt = runtime_factory({"turns": [WRITE, {"text": "done"}]})
    faults.crash_during_write(rt)
    task_id = _crash(rt, "write", EQUALS)
    row = rt.store.ledger_for_task(task_id)[0]
    assert row["status"] == "applying"  # 文件已写、账本未完成

    result = rt.resume(task_id)
    assert result.ok
    reconciled = rt.store.events(task_id, kind="tool.reconciled")
    assert reconciled[0]["data"]["state"] == "after"
    assert rt.store.ledger_for_task(task_id)[0]["status"] == "done"


def test_crash_before_write_reprepares_on_resume(runtime_factory) -> None:
    rt = runtime_factory({"turns": [WRITE, {"text": "done"}]})
    faults.crash_before_tool(rt, "write_file")
    task_id = _crash(rt, "write", EQUALS)
    assert rt.store.ledger_for_task(task_id)[0]["status"] == "pending"
    assert rt.resume(task_id).ok


def test_finished_calls_are_replayed_from_ledger(runtime_factory) -> None:
    calls: list[str] = []
    step = {"tools": [{"tool": "count", "args": {"tag": "a"}}, WRITE]}
    rt = runtime_factory(
        {"turns": [step, {"text": "done"}]}, extra_tools=[counter_tool(calls)]
    )
    faults.crash_before_tool(rt, "write_file")
    task_id = _crash(rt, "x", EQUALS)
    assert calls == ["a"]

    assert rt.resume(task_id).ok
    assert calls == ["a"]  # 第一个工具没有被再执行
    assert rt.store.events(task_id, kind="tool.replayed")


def test_resume_survives_a_new_process(runtime_factory, tmp_path) -> None:
    rt = runtime_factory({"turns": [WRITE, {"text": "done"}]})
    faults.crash_during_write(rt)
    task_id = _crash(rt, "write", EQUALS)
    rt.store.close()

    fresh = runtime_factory({"turns": [WRITE, {"text": "done"}]})
    assert fresh.resume(task_id).ok


def test_external_change_after_crash_is_a_conflict(runtime_factory) -> None:
    rt = runtime_factory({"turns": [WRITE, {"text": "gave up"}]})
    faults.crash_during_write(rt)
    task_id = _crash(rt, "write")
    rt.workspace.write_file("a.txt", "someone else\n")

    result = rt.resume(task_id)
    assert result.ok  # 没有校验规则；模型看到冲突后结束
    assert rt.store.ledger_for_task(task_id)[0]["result"]["code"] == "file_conflict"
    assert rt.workspace.read_file("a.txt") == "someone else\n"


def test_external_edit_before_apply_reports_conflict_and_model_retries(
    runtime_factory,
) -> None:
    script = {
        "turns": [
            {
                "tool": "edit_files",
                "args": {
                    "edits": [{"path": "n.txt", "old_text": "one", "new_text": "two"}]
                },
            },
            {"tool": "read_file", "args": {"path": "n.txt"}},
            {
                "tool": "edit_files",
                "args": {
                    "edits": [{"path": "n.txt", "old_text": "ONE", "new_text": "two"}]
                },
            },
            {"text": "done"},
        ]
    }
    rt = runtime_factory(script)
    rt.workspace.write_file("n.txt", "one\n")
    faults.external_edit(rt, "n.txt", "ONE\n")
    result = rt.run(
        "edit", checks=[{"type": "file_equals", "path": "n.txt", "content": "two\n"}]
    )
    assert result.ok
    assert rt.store.events(result.task_id, kind="tool.conflict")


def test_failure_compensates_in_reverse_and_respects_external_changes(
    runtime_factory,
) -> None:
    script = {
        "turns": [
            WRITE,
            {"tool": "write_file", "args": {"path": "b.txt", "content": "b\n"}},
            {"text": "done"},
        ]
    }
    rt = runtime_factory(script, planning={"max_replans": 0})
    result = rt.run("x", checks=[{"type": "file_absent", "path": "a.txt"}])
    assert result.status == "failed"
    assert not (rt.workspace.root / "a.txt").exists()
    assert not (rt.workspace.root / "b.txt").exists()

    rt2 = runtime_factory(script, planning={"max_replans": 0})
    # 补偿前外部改动了 a.txt：不覆盖，记为冲突
    original = rt2.registry.effect("files")

    class EditThenFail:
        async def apply(self, ctx, data):
            out = await original.apply(ctx, data)
            if "b.txt" in str(data):
                ctx.workspace.write_file("a.txt", "edited by human\n")
            return out

        def probe(self, ctx, data):
            return original.probe(ctx, data)

        def compensate(self, ctx, data):
            original.compensate(ctx, data)

    rt2.registry.add_effect("files", EditThenFail())
    result = rt2.run(
        "y",
        checks=[
            {"type": "file_absent", "path": "zzz"},
            {"type": "file_absent", "path": "b.txt"},
        ],
    )
    assert result.status == "failed"
    assert rt2.workspace.read_file("a.txt") == "edited by human\n"
    assert rt2.store.events(result.task_id, kind="compensation.conflict")


def test_undo_completed_task(runtime_factory) -> None:
    rt = runtime_factory({"turns": [WRITE, {"text": "done"}]})
    result = rt.run("x")
    assert (rt.workspace.root / "a.txt").exists()
    outcomes = rt.compensate(result.task_id)
    assert outcomes[0]["status"] == "compensated"
    assert not (rt.workspace.root / "a.txt").exists()


def test_cancel_stops_and_compensates(runtime_factory) -> None:
    async def run(ctx, args):
        ctx.runtime.cancel(ctx.task_id)
        return "ok"

    stop = Tool("stop", "stop", schema({}, []), "read", run=run)
    rt = runtime_factory(
        {"turns": [WRITE, {"tool": "stop", "args": {}}, {"text": "never"}]},
        extra_tools=[stop],
    )
    result = rt.run("x")
    assert result.status == "cancelled"
    assert not (rt.workspace.root / "a.txt").exists()


def test_cross_process_cancel_interrupts_long_tool(runtime_factory, tmp_path) -> None:
    rt = runtime_factory(
        {"turns": [{"tool": "list_files", "args": {}}, {"text": "never"}]},
        tools={"timeout_s": 30},
    )
    faults.slow_tool(rt, "list_files", delay_s=10)

    async def scenario():
        task = asyncio.create_task(rt.arun("x"))
        await asyncio.sleep(0.3)
        other = Store(rt.settings.database)  # 另一个“进程”的连接
        other.request_cancel(rt.store.tasks(1)[0]["id"])
        return await task

    started = time.monotonic()
    result = asyncio.run(scenario())
    assert result.status == "cancelled"
    assert time.monotonic() - started < 3


def test_timeout_retry_depends_on_recovery(runtime_factory) -> None:
    script = {"turns": [{"tool": "list_files", "args": {}}, {"text": "ok"}]}
    on = runtime_factory(script, tools={"timeout_s": 0.1}, recovery={"tool_retries": 1})
    faults.slow_tool(on, "list_files", delay_s=1)
    result = on.run("x")
    assert on.store.ledger_for_task(result.task_id)[0]["status"] == "done"
    assert len(on.store.events(result.task_id, kind="tool.timeout")) == 1

    off = runtime_factory(script, tools={"timeout_s": 0.1}, recovery={"enabled": False})
    faults.slow_tool(off, "list_files", delay_s=1)
    result = off.run("x")
    assert off.store.ledger_for_task(result.task_id)[0]["result"]["code"] == "timeout"


def test_resume_terminal_task_returns_stored_result(runtime_factory) -> None:
    rt = runtime_factory({"turns": [{"text": "hello"}]})
    first = rt.run("x")
    again = rt.resume(first.task_id)
    assert again.status == "completed" and again.answer == "hello"


def test_bad_tool_call_is_survived(runtime_factory) -> None:
    from minimal_local_agent.model.scripted import scripted_model

    model = faults.BadCallModel(scripted_model({"turns": [WRITE, {"text": "done"}]}))
    rt = runtime_factory({}, models={"default": model})
    result = rt.run("x", checks=EQUALS)
    assert result.ok
    assert rt.store.events(result.task_id, kind="tool.rejected")
