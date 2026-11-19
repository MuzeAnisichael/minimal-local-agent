"""任务状态机：规划 → 执行步骤 → 校验 → 结束（完成 / 失败 / 取消）。

每个步骤 = 一次模型调用 + 它请求的全部工具调用。检查点写在两个位置：
模型响应刚到时（工具尚未执行），以及工具结果全部追加后。进程在任何位置中断，
`resume` 都能从最近的检查点继续，已完成的工具调用由账本去重，不会重复执行。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from minimal_local_agent.core import context as ctxmod
from minimal_local_agent.core.budget import Budget, BudgetExceeded
from minimal_local_agent.core.plan import PLAN_PROMPT, Plan, parse_plan
from minimal_local_agent.runtime.context import (
    CancelToken,
    TaskCancelled,
    ToolContext,
    Tracer,
    digest,
)
from minimal_local_agent.verify.checks import failure_report, run_checks

if TYPE_CHECKING:
    from minimal_local_agent.runtime.runtime import AgentRuntime

Phase = Literal["plan", "act", "verify"]

SYSTEM_PROMPT = """You are a tool-using agent working inside one file workspace.
Rules:
- Use only the tools you are given, with relative workspace paths.
- Never claim a file was read or changed unless a tool result confirms it.
- If a tool returns an error, correct the request when safe. Do not retry a change the
  user denied.
- If a tool reports file_conflict, the file changed underneath you: read it again before
  editing it.
- If a plan is given, keep it current with update_plan.
- When the task is complete, reply with a short final answer and no tool calls.
- Reply in the user's language."""


@dataclass(slots=True)
class TaskState:
    task_id: str
    goal: str
    options: dict[str, Any]
    budget: Budget
    phase: Phase = "plan"
    messages: list[ModelMessage] = field(default_factory=list)
    plan: Plan | None = None
    step_seq: int = 0
    replans: int = 0
    answer: str | None = None
    root_event: int | None = None
    model_event: int | None = None

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "messages": ctxmod.serialize(self.messages),
            "plan": self.plan.to_dict() if self.plan else None,
            "step_seq": self.step_seq,
            "replans": self.replans,
            "answer": self.answer,
            "budget": self.budget.to_dict(),
            "root_event": self.root_event,
            "model_event": self.model_event,
        }

    @classmethod
    def from_checkpoint(
        cls, task_id: str, goal: str, options: dict[str, Any], data: dict[str, Any]
    ) -> TaskState:
        return cls(
            task_id=task_id,
            goal=goal,
            options=options,
            budget=Budget.from_dict(data["budget"]),
            phase=data["phase"],
            messages=ctxmod.deserialize(data["messages"]),
            plan=Plan.from_dict(data.get("plan")),
            step_seq=data["step_seq"],
            replans=data["replans"],
            answer=data.get("answer"),
            root_event=data.get("root_event"),
            model_event=data.get("model_event"),
        )


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    status: str
    answer: str | None
    usage: dict[str, Any]
    verification: list[dict[str, Any]]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "answer": self.answer,
            "usage": self.usage,
            "verification": self.verification,
            "error": self.error,
        }


def _text(response: ModelResponse) -> str:
    return "".join(p.content for p in response.parts if isinstance(p, TextPart)).strip()


def _calls(message: ModelMessage | None) -> list[ToolCallPart]:
    if not isinstance(message, ModelResponse):
        return []
    return [p for p in message.parts if isinstance(p, ToolCallPart)]


class TaskRunner:
    def __init__(
        self,
        runtime: AgentRuntime,
        state: TaskState,
        tracer: Tracer,
        cancel: CancelToken,
    ) -> None:
        self.rt = runtime
        self.state = state
        self.tracer = tracer
        self.cancel = cancel
        self.settings = runtime.settings
        self.verification: list[dict[str, Any]] = []

    # ---- 主循环 ----
    async def run(self) -> TaskResult:
        store = self.rt.store
        store.set_task(self.state.task_id, "running")
        try:
            while True:
                self.cancel.check()
                if self.state.phase == "plan":
                    await self._plan()
                elif self.state.phase == "act":
                    await self._act()
                elif await self._verify():
                    return self._finish("completed")
        except TaskCancelled:
            return self._finish("cancelled", error="cancelled by request")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 预算耗尽、模型全部失败、上下文溢出、校验未通过
            return self._finish("failed", error=f"{type(exc).__name__}: {exc}")

    def checkpoint(self) -> None:
        self.rt.store.save_checkpoint(
            self.state.task_id, self.state.step_seq, self.state.to_checkpoint(), None
        )

    def _tool_context(self, step_id: str) -> ToolContext:
        return ToolContext(
            task_id=self.state.task_id,
            step_id=step_id,
            workspace=self.rt.workspace,
            budget=self.state.budget,
            cancel=self.cancel,
            tracer=self.tracer,
            runtime=self.rt,
        )

    async def _model(
        self, role: str, messages: list[ModelMessage], tools: list, parent: int | None
    ):
        max_tokens = self.state.budget.before_model()

        def failed(name: str, exc: Exception) -> None:
            self.tracer.emit(
                "model.failed",
                {"role": role, "model": name, "error": f"{type(exc).__name__}: {exc}"},
                parent=parent,
            )

        call = await self.rt.router.request(
            role, messages, tools, max_tokens=max_tokens, on_failure=failed
        )
        self.state.budget.charge_model(
            call.input_tokens, call.output_tokens, call.cost_usd, max_tokens
        )
        event = self.tracer.emit(
            "model.called",
            {
                "role": role,
                "model": call.model,
                "input_tokens": call.input_tokens,
                "output_tokens": call.output_tokens,
                "cost_usd": call.cost_usd,
                "latency_ms": call.latency_ms,
                "text": _text(call.response)[:2000],
                "tool_calls": [
                    {"tool": c.tool_name, "call_id": c.tool_call_id}
                    for c in _calls(call.response)
                ],
            },
            parent=parent,
            input_sha=digest(ctxmod.serialize(messages)),
            output_sha=digest(ctxmod.serialize([call.response])),
        )
        return call, event

    # ---- 规划 ----
    async def _plan(self) -> None:
        state = self.state
        notes = [] if state.options.get("depth") else self.rt.memory.recall(state.goal)
        if notes:
            self.tracer.emit("memory.recalled", {"notes": notes})
        if self.settings.planning.enabled:
            tool_names = ", ".join(t.name for t in self.rt.visible_tools(state.options))
            request = ModelRequest(
                parts=[
                    SystemPromptPart(content=PLAN_PROMPT),
                    UserPromptPart(
                        content=f"Task:\n{state.goal}\n\nAvailable tools: {tool_names}"
                    ),
                ]
            )
            call, event = await self._model("plan", [request], [], state.root_event)
            try:
                state.plan = parse_plan(_text(call.response))
                self.rt._plans[state.task_id] = state.plan
                self.tracer.emit("plan.created", state.plan.to_dict(), parent=event)
            except (ValueError, KeyError, TypeError) as exc:
                self.tracer.emit("plan.invalid", {"error": str(exc)}, parent=event)
                state.plan = None
        sections = [f"Task:\n{state.goal}"]
        if notes:
            sections.append(
                "Notes from earlier tasks (reference only, may be outdated):\n"
                + "\n".join(f"- {n}" for n in notes)
            )
        if state.plan:
            sections.append(f"Plan:\n{state.plan.render()}")
        checks = state.options.get("checks") or []
        if checks and state.options.get("reveal_checks", True):
            sections.append(
                "The result will be verified by these checks:\n"
                + "\n".join(f"- {c}" for c in checks)
            )
        state.messages = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content=SYSTEM_PROMPT),
                    UserPromptPart(content="\n\n".join(sections)),
                ]
            )
        ]
        state.phase = "act"
        self.checkpoint()

    # ---- 执行步骤 ----
    async def _act(self) -> None:
        state = self.state
        pending = _calls(state.messages[-1] if state.messages else None)
        if pending:  # 从检查点恢复：上一步的模型响应已保存，工具还没跑完
            await self._run_tools(
                pending, f"{state.task_id}:{state.step_seq}", state.model_event
            )
            return

        state.step_seq += 1
        step_id = f"{state.task_id}:{state.step_seq}"
        self.rt.store.start_step(step_id, state.task_id, state.step_seq, "act")
        step_event = self.tracer.emit(
            "step.started", {"seq": state.step_seq}, step_id=step_id
        )
        await self._fit_context(step_event)

        tools = [
            t.definition()
            for t in self.rt.visible_tools(state.options, plan=state.plan)
        ]
        call, event = await self._model("act", state.messages, tools, step_event)
        state.messages.append(call.response)
        state.model_event = event
        calls = _calls(call.response)
        if calls:
            self.checkpoint()
            await self._run_tools(calls, step_id, event)
            return
        state.answer = _text(call.response)
        state.phase = "verify"
        self.rt.store.finish_step(step_id, "done")
        self.checkpoint()

    async def _run_tools(
        self, calls: list[ToolCallPart], step_id: str, parent: int | None
    ) -> None:
        ctx = self._tool_context(step_id)
        returns = []
        for call in calls:
            self.cancel.check()
            result = await self.rt.executor.execute(ctx, call, parent)
            returns.append(
                ToolReturnPart(
                    tool_name=call.tool_name,
                    content=result,
                    tool_call_id=call.tool_call_id,
                    outcome="success" if result.get("ok", True) else "failed",
                )
            )
        self.state.messages.append(ModelRequest(parts=returns))
        self.rt.store.finish_step(step_id, "done")
        self.checkpoint()

    async def _fit_context(self, parent: int) -> None:
        cfg = self.settings.context
        summarize = None
        if cfg.compaction == "summarize":
            summarize = self._summarizer(parent)
        compacted = await ctxmod.fit(
            self.state.messages,
            cfg.max_tokens,
            cfg.keep_recent_steps,
            summarize=summarize,
            extra_note=self.state.plan.render() if self.state.plan else "",
        )
        if compacted is not None:
            self.state.messages = compacted.messages
            self.tracer.emit(
                "context.compacted",
                {
                    "mode": cfg.compaction,
                    "before_tokens": compacted.before_tokens,
                    "after_tokens": compacted.after_tokens,
                    "dropped_steps": compacted.dropped_steps,
                },
                parent=parent,
            )

    def _summarizer(self, parent: int):
        async def summarize(units: list[list[ModelMessage]]) -> str:
            transcript = ctxmod.digest_units(units)
            request = ModelRequest(
                parts=[
                    SystemPromptPart(
                        content="Summarize what these agent steps did and found, in under "
                        "120 words. Keep file paths and key facts."
                    ),
                    UserPromptPart(content=transcript),
                ]
            )
            call, _ = await self._model("summarize", [request], [], parent)
            return _text(call.response) or transcript

        return summarize

    # ---- 校验 ----
    async def _verify(self) -> bool:
        state = self.state
        checks = state.options.get("checks") or []
        results = await asyncio.to_thread(
            run_checks, checks, self.rt.workspace, state.answer or ""
        )
        self.verification = [r.to_dict() for r in results]
        passed = all(r.passed for r in results)
        event = self.tracer.emit(
            "verify.passed" if passed else "verify.failed",
            {"results": self.verification, "replans": state.replans},
        )
        for r in results:
            if r.artifact_sha:
                self.tracer.emit(
                    "artifact.verified",
                    {"passed": r.passed},
                    parent=event,
                    artifact_path=r.artifact_path,
                    artifact_sha=r.artifact_sha,
                )
        if passed:
            return True
        if state.replans >= self.settings.planning.max_replans:
            raise VerificationFailed(failure_report(results))
        state.replans += 1
        state.messages.append(
            ModelRequest(
                parts=[
                    UserPromptPart(
                        content="Verification failed:\n"
                        + failure_report(results)
                        + "\nFix the problems with tools, then reply with your final answer."
                    )
                ]
            )
        )
        state.phase = "act"
        self.checkpoint()
        return False

    # ---- 结束 ----
    def _finish(self, status: str, *, error: str | None = None) -> TaskResult:
        state = self.state
        if status != "completed":
            self.rt.compensate(state.task_id, self.tracer)
        result = TaskResult(
            task_id=state.task_id,
            status=status,
            answer=state.answer,
            usage=state.budget.used.to_dict(),
            verification=self.verification,
            error=error,
        )
        self.rt.store.set_task(
            state.task_id, status, result=result.to_dict(), error=error
        )
        self.tracer.emit(f"task.{status}", {"error": error, "usage": result.usage})
        if not state.options.get("depth"):
            self.rt.memory.record(state.task_id, state.goal, result)
        return result


class VerificationFailed(RuntimeError):
    pass


__all__ = [
    "SYSTEM_PROMPT",
    "BudgetExceeded",
    "TaskResult",
    "TaskRunner",
    "TaskState",
    "VerificationFailed",
]
