"""运行期上下文：事件记录器、取消令牌，以及传给每个工具的 ToolContext。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from minimal_local_agent.runtime.store import Store

if TYPE_CHECKING:
    from minimal_local_agent.core.budget import Budget
    from minimal_local_agent.runtime.runtime import AgentRuntime
    from minimal_local_agent.tools.fs import Workspace


def digest(value: Any) -> str:
    """稳定哈希：用于幂等键和事件里的输入/输出指纹。"""

    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


EventSink = Callable[[dict[str, Any]], None]


@dataclass(slots=True)
class Tracer:
    """把事件写进存储；可选的 sink 只用于实时显示，出错不影响执行。"""

    store: Store
    task_id: str
    root_id: int | None = None
    sink: EventSink | None = None

    def emit(
        self,
        kind: str,
        data: dict[str, Any] | None = None,
        *,
        parent: int | None = None,
        step_id: str | None = None,
        **fields: Any,
    ) -> int:
        data = data or {}
        event_id = self.store.add_event(
            self.task_id,
            kind,
            data,
            step_id=step_id,
            parent_id=parent if parent is not None else self.root_id,
            **fields,
        )
        if self.sink is not None:
            # 显示失败不能改变任务结果
            with contextlib.suppress(Exception):
                self.sink(
                    {"id": event_id, "task_id": self.task_id, "kind": kind, **data}
                )
        return event_id


class TaskCancelled(Exception):
    pass


@dataclass(slots=True)
class CancelToken:
    """进程内的取消事件 + 存储里的取消标记（支持另一个进程执行 `mla cancel`）。"""

    store: Store
    task_id: str
    parent: CancelToken | None = None
    poll_s: float = 0.2
    _event: asyncio.Event = field(default_factory=asyncio.Event)

    def cancel(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        if self._event.is_set():
            return True
        if self.store.cancel_requested(self.task_id) or (
            self.parent is not None and self.parent.is_set()
        ):
            self._event.set()
            return True
        return False

    def check(self) -> None:
        if self.is_set():
            raise TaskCancelled(self.task_id)

    async def wait(self) -> None:
        while not self.is_set():
            try:
                await asyncio.wait_for(self._event.wait(), timeout=self.poll_s)
            except TimeoutError:
                continue


@dataclass(slots=True)
class ToolContext:
    task_id: str
    step_id: str
    workspace: Workspace
    budget: Budget
    cancel: CancelToken
    tracer: Tracer
    runtime: AgentRuntime
    # 由执行入口填写：本次调用的幂等键与 tool.called 事件 ID。
    call_key: str = ""
    event_id: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "CancelToken",
    "EventSink",
    "TaskCancelled",
    "ToolContext",
    "Tracer",
    "digest",
]
