"""事件轨迹查询：按父子关系打印任务的事件树，或从一个产物回溯到产生它的模型调用和原始输入。"""

from __future__ import annotations

import json
from typing import Any

from minimal_local_agent.runtime.store import Store

_BRIEF = {
    "task.started": lambda d: d.get("goal", "")[:80],
    "plan.created": lambda d: f"{len(d.get('steps', []))} steps",
    "step.started": lambda d: f"#{d.get('seq')}",
    "model.called": lambda d: (
        f"{d.get('role')} via {d.get('model')} "
        f"in={d.get('input_tokens')} out={d.get('output_tokens')} "
        + ", ".join(c["tool"] for c in d.get("tool_calls", []))
    ),
    "tool.called": lambda d: (
        f"{d.get('tool')} {json.dumps(d.get('args'), ensure_ascii=False)[:80]}"
    ),
    "tool.completed": lambda d: f"{d.get('tool')} {d.get('status')}",
    "artifact.written": lambda d: d.get("path", ""),
    "verify.passed": lambda d: f"{len(d.get('results', []))} checks",
    "verify.failed": lambda d: "; ".join(
        r["detail"] for r in d.get("results", []) if not r.get("passed")
    )[:120],
}


def brief(event: dict[str, Any]) -> str:
    data = event.get("data") or {}
    fn = _BRIEF.get(event["kind"])
    if fn is not None:
        try:
            return str(fn(data))
        except Exception:
            pass
    keys = [
        k for k in ("tool", "state", "status", "error", "subtask_id") if data.get(k)
    ]
    return " ".join(f"{k}={data[k]}" for k in keys)


def tree(store: Store, task_id: str) -> list[str]:
    events = store.events(task_id)
    children: dict[int | None, list[dict[str, Any]]] = {}
    ids = {e["id"] for e in events}
    for event in events:
        parent = event["parent_id"] if event["parent_id"] in ids else None
        children.setdefault(parent, []).append(event)
    lines: list[str] = []

    def walk(parent: int | None, depth: int) -> None:
        for event in children.get(parent, []):
            lines.append(
                f"{'  ' * depth}#{event['id']} {event['kind']}  {brief(event)}"
            )
            walk(event["id"], depth + 1)

    walk(None, 0)
    return lines


def lineage(store: Store, event_id: int) -> list[dict[str, Any]]:
    """从某个事件沿 parent_id 一直走到任务根事件。"""

    chain: list[dict[str, Any]] = []
    current = store.event(event_id)
    while current is not None:
        chain.append(current)
        parent = current["parent_id"]
        current = store.event(parent) if parent is not None else None
    return chain


def artifact(
    store: Store, *, path: str | None = None, sha: str | None = None
) -> list[dict[str, Any]]:
    """找到最近一次写出该产物的事件，并返回它的完整来源链（产物 → 工具 → 模型 → 步骤 → 任务）。"""

    for event in store.artifact_events(path=path, sha=sha):
        if event["kind"] == "artifact.written":
            return lineage(store, event["id"])
    return []


__all__ = ["artifact", "brief", "lineage", "tree"]
