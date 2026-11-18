"""统一的工具执行入口：幂等账本、审批、超时、取消、重试、对账与事件记录。

每次工具调用的幂等键 = hash(任务, 步骤, 调用 ID, 工具名, 规范化参数)。
账本状态：pending（已登记）→ applying（副作用数据已落账，正在落地）→ done / failed，
补偿后变为 compensated。恢复时：
- done / failed：直接返回账本里的结果，不重复执行；
- pending：副作用尚未落地，只读工具重跑，写工具重新 prepare；
- applying：用 EffectHandler.probe 对账，写后状态补记完成，写前/部分状态继续落地，
  冲突则报告 file_conflict，结果未知的外部写操作只有声明 replay_safe 才重放。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

from pydantic_ai.messages import ToolCallPart

from minimal_local_agent.config import RecoveryConfig, ToolsConfig
from minimal_local_agent.core.budget import BudgetExceeded
from minimal_local_agent.policy import Approver, Policy
from minimal_local_agent.runtime.context import TaskCancelled, ToolContext, digest
from minimal_local_agent.runtime.store import Store
from minimal_local_agent.tools.files import FileConflictError
from minimal_local_agent.tools.registry import Tool, ToolRegistry

FINISHED = frozenset({"done", "failed", "compensated"})


class ToolTimeout(TimeoutError):
    pass


class OutcomeUnknown(RuntimeError):
    pass


def call_key(task_id: str, step_id: str, call_id: str, tool: str, args: Any) -> str:
    return digest([task_id, step_id, call_id, tool, args])


async def guarded(ctx: ToolContext, awaitable: Any, timeout: float | None) -> Any:
    """运行协程，同时等待取消信号；超时抛 ToolTimeout，取消抛 TaskCancelled。"""

    work = asyncio.ensure_future(awaitable)
    stop = asyncio.ensure_future(ctx.cancel.wait())
    try:
        done, _ = await asyncio.wait(
            {work, stop}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        stop.cancel()
    if work in done:
        return work.result()
    work.cancel()
    if stop in done:
        raise TaskCancelled(ctx.task_id)
    raise ToolTimeout(f"Tool timed out after {timeout}s")


class ToolExecutor:
    def __init__(
        self,
        store: Store,
        registry: ToolRegistry,
        policy: Policy,
        approver: Approver,
        tools: ToolsConfig,
        recovery: RecoveryConfig,
    ) -> None:
        self.store = store
        self.registry = registry
        self.policy = policy
        self.approver = approver
        self.tools = tools
        self.recovery = recovery

    # ---- 对外入口 ----
    async def execute(
        self, ctx: ToolContext, call: ToolCallPart, parent: int | None
    ) -> dict[str, Any]:
        tool = self.registry.get(call.tool_name)
        try:
            raw = call.args
            args = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except Exception as exc:
            return self._reject(ctx, call, parent, f"Invalid JSON arguments: {exc}")
        if tool is None or self.policy.decision(tool) == "deny":
            return self._reject(ctx, call, parent, f"Unknown tool: {call.tool_name}")

        key = call_key(ctx.task_id, ctx.step_id, call.tool_call_id, tool.name, args)
        row = self.store.ledger(key)
        # 控制工具只改内存里的任务状态（如计划），恢复时重跑以重建状态；它们本身是幂等的。
        if row is not None and row["status"] in FINISHED and tool.effect != "control":
            ctx.tracer.emit(
                "tool.replayed",
                {"tool": tool.name, "key": key, "status": row["status"]},
                parent=parent,
                step_id=ctx.step_id,
            )
            return row["result"]

        if row is None:
            ctx.budget.before_tool()  # 超额时直接抛 BudgetExceeded，不登记账本
            self.store.ledger_open(
                key,
                ctx.task_id,
                ctx.step_id,
                call.tool_call_id,
                tool.name,
                digest(args),
            )
        called = ctx.tracer.emit(
            "tool.called",
            {
                "tool": tool.name,
                "args": args,
                "call_id": call.tool_call_id,
                "key": key,
                "effect": tool.effect,
                "resumed": row is not None,
            },
            parent=parent,
            step_id=ctx.step_id,
            input_sha=digest(args),
        )
        tctx = replace(ctx, call_key=key, event_id=called)
        try:
            if tool.effect == "write":
                result, summary = await self._write(tctx, tool, args, row)
            else:
                result, summary = await self._run(tctx, tool, args), {}
            status = "done"
        except (TaskCancelled, BudgetExceeded):
            self.store.ledger_update(key, status="failed", error="interrupted")
            raise
        except FileConflictError as exc:
            result, summary, status = self._error("file_conflict", exc), {}, "failed"
            ctx.tracer.emit(
                "tool.conflict",
                {"tool": tool.name, "paths": exc.paths},
                parent=called,
                step_id=ctx.step_id,
            )
        except Exception as exc:
            code = "timeout" if isinstance(exc, ToolTimeout) else "tool_error"
            result, summary, status = self._error(code, exc), {}, "failed"

        result = self._bounded(result)
        self.store.ledger_update(key, status=status, result=result)
        completed = ctx.tracer.emit(
            "tool.completed",
            {"tool": tool.name, "status": status, "result": result},
            parent=called,
            step_id=ctx.step_id,
            output_sha=digest(result),
        )
        for item in summary.get("files", []):
            if item.get("after_sha"):
                ctx.tracer.emit(
                    "artifact.written",
                    {"tool": tool.name, "path": item["path"]},
                    parent=completed,
                    step_id=ctx.step_id,
                    artifact_path=item["path"],
                    artifact_sha=item["after_sha"],
                )
        return result

    # ---- 只读 / 控制 / 子任务工具 ----
    async def _run(self, ctx: ToolContext, tool: Tool, args: dict[str, Any]) -> Any:
        assert tool.run is not None
        attempts = 1 + (self.recovery.tool_retries if self.recovery.enabled else 0)
        timeout = tool.timeout_s or self.tools.timeout_s
        for attempt in range(1, attempts + 1):
            try:
                value = await guarded(ctx, tool.run(ctx, args), timeout)
                return {"ok": True, "result": value}
            except ToolTimeout:
                ctx.tracer.emit(
                    "tool.timeout",
                    {"tool": tool.name, "attempt": attempt, "timeout_s": timeout},
                    parent=ctx.event_id,
                    step_id=ctx.step_id,
                )
                if attempt == attempts:
                    raise
        raise AssertionError("unreachable")

    # ---- 有副作用的工具 ----
    async def _write(
        self,
        ctx: ToolContext,
        tool: Tool,
        args: dict[str, Any],
        row: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        assert tool.prepare is not None
        if row is not None and row["status"] == "applying" and row.get("prepared"):
            return await self._reconcile(ctx, tool, row["prepared"])

        timeout = tool.timeout_s or self.tools.timeout_s
        prepared = await guarded(ctx, tool.prepare(ctx, args), timeout)
        handler = self.registry.effect(prepared.kind)
        decision = self.policy.decision(tool)
        if decision == "preview":
            return {
                "ok": True,
                "applied": False,
                "message": "Preview only; nothing was changed.",
                "diff": prepared.preview,
            }, {}
        if decision == "ask" and not self.approver(tool.name, prepared.preview):
            ctx.tracer.emit(
                "tool.denied",
                {"tool": tool.name},
                parent=ctx.event_id,
                step_id=ctx.step_id,
            )
            return {
                "ok": False,
                "code": "approval_denied",
                "error": "The user did not approve this change. Do not retry it.",
            }, {}
        ctx.cancel.check()
        # 先把副作用数据落账，再落盘：崩溃后恢复逻辑能据此对账或补偿。
        self.store.ledger_update(
            ctx.call_key,
            status="applying",
            prepared={
                "kind": prepared.kind,
                "data": prepared.data,
                "summary": prepared.summary,
            },
        )
        applied = await handler.apply(ctx, prepared.data)
        return {"ok": True, **(applied or {})}, prepared.summary

    async def _reconcile(
        self, ctx: ToolContext, tool: Tool, prepared: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        handler = self.registry.effect(prepared["kind"])
        state = handler.probe(ctx, prepared["data"])
        ctx.tracer.emit(
            "tool.reconciled",
            {"tool": tool.name, "state": state},
            parent=ctx.event_id,
            step_id=ctx.step_id,
        )
        summary = prepared.get("summary", {})
        if state == "after":
            return {"ok": True, "applied": True, "reconciled": True, **summary}, summary
        if state in {"before", "partial"} or (state == "unknown" and tool.replay_safe):
            applied = await handler.apply(ctx, prepared["data"])
            return {"ok": True, "reconciled": True, **(applied or {})}, summary
        if state == "conflict":
            raise FileConflictError(summary.get("paths", []))
        raise OutcomeUnknown(
            f"{tool.name} may or may not have run before the interruption; "
            "it is not declared replay-safe, so it was not repeated"
        )

    # ---- 辅助 ----
    def _reject(
        self, ctx: ToolContext, call: ToolCallPart, parent: int | None, message: str
    ) -> dict[str, Any]:
        ctx.tracer.emit(
            "tool.rejected",
            {"tool": call.tool_name, "error": message},
            parent=parent,
            step_id=ctx.step_id,
        )
        return {"ok": False, "code": "invalid_call", "error": message}

    @staticmethod
    def _error(code: str, exc: Exception) -> dict[str, Any]:
        return {"ok": False, "code": code, "error": f"{type(exc).__name__}: {exc}"}

    def _bounded(self, result: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(result, ensure_ascii=False, default=str)
        limit = self.tools.max_result_chars
        if len(text) <= limit:
            return json.loads(text)
        return {
            "ok": result.get("ok", True),
            "truncated": True,
            "content": text[:limit],
        }


__all__ = ["OutcomeUnknown", "ToolExecutor", "ToolTimeout", "call_key", "guarded"]
