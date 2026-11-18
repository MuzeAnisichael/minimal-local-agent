"""任务计划：计划是任务状态的一部分，执行中通过 update_plan 工具更新，并写入检查点。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from minimal_local_agent.runtime.context import ToolContext
from minimal_local_agent.tools.registry import Tool, schema

StepStatus = Literal["pending", "in_progress", "done", "skipped"]

PLAN_PROMPT = """You are planning a task for a tool-using agent working inside a file workspace.
Return ONLY a JSON object, no prose, in this shape:
{"steps": [{"id": "s1", "title": "...", "done_when": "observable completion condition"}]}
Use 1 to 6 short steps. Each done_when must be checkable from files or tool results.
"""


@dataclass(slots=True)
class PlanStep:
    id: str
    title: str
    done_when: str = ""
    status: StepStatus = "pending"
    note: str = ""


@dataclass(slots=True)
class Plan:
    steps: list[PlanStep] = field(default_factory=list)
    revision: int = 0

    def render(self) -> str:
        if not self.steps:
            return "(no plan)"
        return "\n".join(
            f"- [{s.status}] {s.id}: {s.title}"
            + (f" (done when: {s.done_when})" if s.done_when else "")
            + (f" — {s.note}" if s.note else "")
            for s in self.steps
        )

    def to_dict(self) -> dict[str, Any]:
        return {"steps": [asdict(s) for s in self.steps], "revision": self.revision}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Plan | None:
        if not data:
            return None
        return cls(
            [PlanStep(**s) for s in data.get("steps", [])], data.get("revision", 0)
        )


def parse_plan(text: str) -> Plan:
    """从模型输出里取第一个 JSON 对象；格式不对时抛 ValueError。"""

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object in planner output")
    data = json.loads(match.group(0))
    raw = data.get("steps")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Planner output has no steps")
    steps = []
    for index, item in enumerate(raw[:8], start=1):
        if not isinstance(item, dict) or not str(item.get("title", "")).strip():
            raise ValueError("Each plan step needs a title")
        steps.append(
            PlanStep(
                id=str(item.get("id") or f"s{index}")[:16],
                title=str(item["title"])[:200],
                done_when=str(item.get("done_when", ""))[:300],
            )
        )
    return Plan(steps)


def update_plan_tool(get_plan: Any) -> Tool:
    """控制工具：让模型标记步骤进度或追加步骤。get_plan() 返回当前任务的 Plan。"""

    async def run(ctx: ToolContext, args: dict[str, Any]) -> Any:
        plan: Plan | None = get_plan(ctx.task_id)
        if plan is None:
            raise ValueError("This task has no plan")
        step = next((s for s in plan.steps if s.id == args["step_id"]), None)
        if step is None:
            if not args.get("title"):
                raise ValueError(
                    f"Unknown step {args['step_id']}; give a title to add it"
                )
            step = PlanStep(
                id=str(args["step_id"])[:16], title=str(args["title"])[:200]
            )
            plan.steps.append(step)
        step.status = args.get("status", step.status)
        step.note = str(args.get("note", step.note))[:300]
        plan.revision += 1
        return plan.render()

    return Tool(
        "update_plan",
        "Update the status of a plan step, or add a new step (give title).",
        schema(
            {
                "step_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "skipped"],
                },
                "title": {"type": "string"},
                "note": {"type": "string"},
            },
            ["step_id"],
        ),
        "control",
        run=run,
    )


__all__ = ["PLAN_PROMPT", "Plan", "PlanStep", "parse_plan", "update_plan_tool"]
