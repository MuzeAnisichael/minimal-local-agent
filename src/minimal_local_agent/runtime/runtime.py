"""AgentRuntime：组装存储、工作区、模型路由、工具与执行入口，对外提供 run / resume / cancel。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic_ai.models import Model

from minimal_local_agent.config import Settings
from minimal_local_agent.core.budget import Budget, Limits
from minimal_local_agent.core.loop import TaskResult, TaskRunner, TaskState
from minimal_local_agent.core.plan import Plan, update_plan_tool
from minimal_local_agent.memory.strategies import build_memory
from minimal_local_agent.model.adapter import ModelRouter
from minimal_local_agent.policy import Approver, Policy, deny_all
from minimal_local_agent.runtime.context import (
    CancelToken,
    EventSink,
    ToolContext,
    Tracer,
    digest,
)
from minimal_local_agent.runtime.executor import ToolExecutor
from minimal_local_agent.runtime.recovery import compensate_task
from minimal_local_agent.runtime.store import TERMINAL, Store
from minimal_local_agent.tools.fs import Workspace
from minimal_local_agent.tools.registry import EffectHandler, Tool, ToolRegistry
from minimal_local_agent.tools.workspace import FILES, FilesEffect, workspace_tools


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        models: Mapping[str, Model] | None = None,
        approver: Approver | None = None,
        tools: Sequence[Tool] = (),
        effects: Mapping[str, EffectHandler] | None = None,
        on_event: EventSink | None = None,
        store: Store | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or Store(settings.database)
        cfg = settings.tools
        self.workspace = Workspace.at(
            settings.workspace.root,
            max_file_bytes=cfg.max_file_bytes,
            max_list_results=cfg.max_list_results,
            max_search_results=cfg.max_search_results,
            max_search_files=cfg.max_search_files,
        )
        self.router = ModelRouter.from_config(settings.models, settings.routing, models)
        self.policy = Policy(settings.policy)
        self.on_event = on_event
        self.memory = build_memory(
            self.store, settings.memory, str(self.workspace.root)
        )
        self._plans: dict[str, Plan] = {}
        self._cancels: dict[str, CancelToken] = {}
        self._mcp: Any = None

        self.registry = ToolRegistry()
        self.registry.add_effect(FILES, FilesEffect())
        for kind, handler in (effects or {}).items():
            self.registry.add_effect(kind, handler)
        self.registry.extend(workspace_tools(cfg))
        self.registry.add(update_plan_tool(self._plans.get))
        from minimal_local_agent.tools.subtask import subtask_tool

        self.registry.add(subtask_tool())
        self.registry.extend(tools)
        self.executor = ToolExecutor(
            self.store,
            self.registry,
            self.policy,
            approver or deny_all,
            cfg,
            settings.recovery,
        )

    # ---- 工具可见性 ----
    def visible_tools(
        self, options: dict[str, Any], plan: Plan | None = None
    ) -> list[Tool]:
        allowed = options.get("tools")
        result = []
        for tool in self.policy.visible(list(self.registry)):
            if tool.name == "update_plan" and plan is None:
                continue
            if tool.name == "run_subtask" and options.get("depth", 0) >= 1:
                continue  # 子任务不能再派生子任务
            if (
                allowed is not None
                and tool.name not in allowed
                and tool.effect != "control"
            ):
                continue
            result.append(tool)
        return result

    # ---- 任务生命周期 ----
    def _tracer(self, task_id: str, root: int | None = None) -> Tracer:
        return Tracer(self.store, task_id, root, self.on_event)

    def _token(self, task_id: str, parent: CancelToken | None = None) -> CancelToken:
        token = CancelToken(self.store, task_id, parent)
        self._cancels[task_id] = token
        return token

    async def _ensure_mcp(self) -> None:
        if self._mcp is not None or not self.settings.mcp.servers:
            return
        from minimal_local_agent.tools.mcp import MCPConnector

        self._mcp = MCPConnector(self.settings.mcp, self.settings.tools)
        tools, effects = await self._mcp.connect()
        for kind, handler in effects.items():
            self.registry.add_effect(kind, handler)
        self.registry.extend(tools)

    async def aclose(self) -> None:
        if self._mcp is not None:
            await self._mcp.close()
            self._mcp = None

    def new_task(
        self,
        goal: str,
        *,
        checks: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
        parent_id: str | None = None,
        task_id: str | None = None,
        limits: Limits | None = None,
    ) -> TaskState:
        if not goal.strip():
            raise ValueError("Goal cannot be empty")
        task_id = task_id or f"task-{uuid.uuid4().hex[:12]}"
        opts = {**(options or {}), "checks": list(checks or [])}
        budget = Budget(
            limits or Limits.from_config(self.settings.budget),
            self.settings.budget.max_output_tokens_per_request,
        )
        self.store.create_task(task_id, goal, opts, parent_id)
        state = TaskState(task_id=task_id, goal=goal, options=opts, budget=budget)
        tracer = self._tracer(task_id)
        state.root_event = tracer.emit(
            "task.started",
            {
                "goal": goal,
                "options": opts,
                "parent_id": parent_id,
                "limits": budget.to_dict()["limits"],
            },
            input_sha=digest(goal),
        )
        self.store.save_checkpoint(task_id, 0, state.to_checkpoint(), state.root_event)
        return state

    async def _drive(
        self, state: TaskState, parent_cancel: CancelToken | None = None
    ) -> TaskResult:
        await self._ensure_mcp()
        if state.plan is not None:
            self._plans[state.task_id] = state.plan
        tracer = self._tracer(state.task_id, state.root_event)
        runner = TaskRunner(
            self, state, tracer, self._token(state.task_id, parent_cancel)
        )
        try:
            result = await runner.run()
        finally:
            self._cancels.pop(state.task_id, None)
        return result

    async def arun(
        self,
        goal: str,
        *,
        checks: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> TaskResult:
        return await self._drive(self.new_task(goal, checks=checks, options=options))

    def load_state(self, task_id: str) -> TaskState:
        task = self.store.task(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        checkpoint = self.store.latest_checkpoint(task_id)
        if checkpoint is None:
            raise RuntimeError(f"Task {task_id} has no checkpoint")
        return TaskState.from_checkpoint(
            task_id, task["goal"], task["options"], checkpoint["state"]
        )

    async def aresume(
        self, task_id: str, parent_cancel: CancelToken | None = None
    ) -> TaskResult:
        task = self.store.task(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        if task["status"] in TERMINAL:
            return self.result(task_id)
        state = self.load_state(task_id)
        self._tracer(task_id, state.root_event).emit(
            "task.resumed", {"phase": state.phase, "step_seq": state.step_seq}
        )
        return await self._drive(state, parent_cancel)

    def result(self, task_id: str) -> TaskResult:
        task = self.store.task(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        data = task.get("result") or {}
        return TaskResult(
            task_id=task_id,
            status=task["status"],
            answer=data.get("answer"),
            usage=data.get("usage", {}),
            verification=data.get("verification", []),
            error=task.get("error"),
        )

    def cancel(self, task_id: str) -> bool:
        token = self._cancels.get(task_id)
        if token is not None:
            token.cancel()
        return self.store.request_cancel(task_id)

    def compensate(
        self, task_id: str, tracer: Tracer | None = None
    ) -> list[dict[str, Any]]:
        recovery = self.settings.recovery
        if not (recovery.enabled and recovery.compensate_on_failure):
            return []
        tracer = tracer or self._tracer(task_id)
        ctx = ToolContext(
            task_id=task_id,
            step_id=f"{task_id}:compensate",
            workspace=self.workspace,
            budget=Budget(Limits(0, 0, 0)),
            cancel=CancelToken(self.store, task_id),
            tracer=tracer,
            runtime=self,
        )
        return compensate_task(self.store, self.registry, ctx, task_id, tracer)

    # ---- 同步入口（CLI 与脚本使用） ----
    def _sync(self, coroutine: Any) -> Any:
        async def main() -> Any:
            try:
                return await coroutine
            finally:
                await self.aclose()

        return asyncio.run(main())

    def run(self, goal: str, **kwargs: Any) -> TaskResult:
        return self._sync(self.arun(goal, **kwargs))

    def resume(self, task_id: str) -> TaskResult:
        return self._sync(self.aresume(task_id))


__all__ = ["AgentRuntime"]
