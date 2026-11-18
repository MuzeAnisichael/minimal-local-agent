"""补偿：任务失败或取消时，按逆序撤销账本里已落地的副作用。

只依赖账本里的 prepared 数据，所以即使是另一个进程、或重启之后，也能补偿。
被外部改动过的文件不会被覆盖，记为 compensation.conflict 留给人处理。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from minimal_local_agent.runtime.context import Tracer
from minimal_local_agent.tools.files import FileConflictError

if TYPE_CHECKING:
    from minimal_local_agent.runtime.context import ToolContext
    from minimal_local_agent.runtime.store import Store
    from minimal_local_agent.tools.registry import ToolRegistry


def compensate_task(
    store: Store,
    registry: ToolRegistry,
    ctx: ToolContext,
    task_id: str,
    tracer: Tracer,
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    # 子任务先于父任务补偿（子任务的写入发生在父任务的调用内部）。
    for child in store.tasks(parent_id=task_id):
        outcomes += compensate_task(store, registry, ctx, child["id"], tracer)
    for row in reversed(store.ledger_for_task(task_id)):
        prepared = row.get("prepared")
        if row["status"] not in {"done", "applying"} or not isinstance(prepared, dict):
            continue
        handler = registry.effect(prepared["kind"])
        paths = prepared.get("summary", {}).get("paths", [])
        try:
            handler.compensate(ctx, prepared["data"])
        except FileConflictError as exc:
            outcome = {
                "key": row["key"],
                "tool": row["tool"],
                "status": "conflict",
                "paths": exc.paths,
            }
            tracer.emit("compensation.conflict", outcome)
        except NotImplementedError:
            outcome = {"key": row["key"], "tool": row["tool"], "status": "unsupported"}
            tracer.emit("compensation.skipped", outcome)
        else:
            store.ledger_update(row["key"], status="compensated")
            outcome = {
                "key": row["key"],
                "tool": row["tool"],
                "status": "compensated",
                "paths": paths,
            }
            tracer.emit("compensation.applied", outcome)
        outcomes.append(outcome)
    return outcomes


__all__ = ["compensate_task"]
