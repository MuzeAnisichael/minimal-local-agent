"""受控子任务：子任务也是一个工具调用。

- 子任务 ID 由父任务的幂等键派生，父任务恢复时会接着跑同一个子任务，而不是重开一个；
- 预算从父账本切出，结束后按实际用量结算；
- 子任务继承父任务的取消令牌，父任务取消时一起停止；
- 子任务只能使用父任务可见工具的子集（默认只读），且不能再派生子任务。
"""

from __future__ import annotations

from typing import Any

from minimal_local_agent.core.budget import Usage
from minimal_local_agent.runtime.context import ToolContext
from minimal_local_agent.runtime.store import TERMINAL
from minimal_local_agent.tools.registry import Tool, schema

READ_ONLY = ["list_files", "read_file", "search_text"]


def subtask_tool(timeout_s: float = 900.0) -> Tool:
    async def run(ctx: ToolContext, args: dict[str, Any]) -> Any:
        rt = ctx.runtime
        parent = rt.store.task(ctx.task_id) or {}
        options = parent.get("options") or {}
        child_id = f"{ctx.task_id}.sub-{ctx.call_key[:10]}"
        existing = rt.store.task(child_id)

        if existing is None:
            visible = {t.name for t in rt.visible_tools(options)}
            wanted = list(args.get("tools") or READ_ONLY)
            illegal = [n for n in wanted if n not in visible or n == "run_subtask"]
            if illegal:
                raise ValueError(f"Subtask cannot use tools: {illegal}")
            limits = ctx.budget.allocate(
                child_id,
                requests=int(args.get("max_requests", 6)),
                tool_calls=int(args.get("max_tool_calls", 12)),
                output_tokens=int(args.get("max_output_tokens", 4000)),
            )
            state = rt.new_task(
                args["goal"],
                checks=list(args.get("checks") or []),
                options={"tools": wanted, "depth": options.get("depth", 0) + 1},
                parent_id=ctx.task_id,
                task_id=child_id,
                limits=limits,
            )
            ctx.tracer.emit(
                "subtask.started",
                {"subtask_id": child_id, "goal": args["goal"], "tools": wanted},
                parent=ctx.event_id,
                step_id=ctx.step_id,
            )
            result = await rt._drive(state, ctx.cancel)
        else:
            if existing["status"] not in TERMINAL:
                # 父任务从检查点恢复后，账本里的预留需要按子任务自己的上限重新登记。
                ctx.budget.reserved[child_id] = rt.load_state(child_id).budget.limits
            ctx.tracer.emit(
                "subtask.resumed",
                {"subtask_id": child_id},
                parent=ctx.event_id,
                step_id=ctx.step_id,
            )
            result = await rt.aresume(child_id, ctx.cancel)

        ctx.budget.settle(
            child_id, Usage.from_dict(result.usage) if result.usage else Usage()
        )
        return {
            "subtask_id": child_id,
            "status": result.status,
            "answer": result.answer,
            "checks_passed": all(v.get("passed") for v in result.verification),
            "error": result.error,
        }

    return Tool(
        "run_subtask",
        "Delegate a self-contained subtask to a child agent with its own budget. "
        "Only a returned status of completed counts as done. tools defaults to read-only.",
        schema(
            {
                "goal": {"type": "string"},
                "tools": {"type": "array", "items": {"type": "string"}},
                "checks": {"type": "array", "items": {"type": "object"}},
                "max_requests": {"type": "integer", "minimum": 1},
                "max_tool_calls": {"type": "integer", "minimum": 0},
                "max_output_tokens": {"type": "integer", "minimum": 1},
            },
            ["goal"],
        ),
        "subtask",
        run=run,
        timeout_s=timeout_s,
    )


__all__ = ["READ_ONLY", "subtask_tool"]
