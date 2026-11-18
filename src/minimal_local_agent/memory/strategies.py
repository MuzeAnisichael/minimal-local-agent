"""记忆策略：可替换、可在评测里对比。

- none：不跨任务保留任何东西（基础循环）。
- episodic：每个任务结束后记一条经验（目标、结果、校验失败原因），
  新任务开始时按关键词重叠检索最相关的几条，作为参考注入开头提示。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from minimal_local_agent.config import MemoryConfig

if TYPE_CHECKING:
    from minimal_local_agent.core.loop import TaskResult
    from minimal_local_agent.runtime.store import Store

_WORD = re.compile(r"[A-Za-z0-9_./-]{3,}|[一-鿿]")


def keywords(text: str) -> set[str]:
    return {w.casefold() for w in _WORD.findall(text)}


class MemoryStrategy(Protocol):
    def recall(self, goal: str) -> list[str]: ...

    def record(self, task_id: str, goal: str, result: TaskResult) -> None: ...


class NoMemory:
    def recall(self, goal: str) -> list[str]:
        return []

    def record(self, task_id: str, goal: str, result: TaskResult) -> None:
        return None


class EpisodicMemory:
    def __init__(self, store: Store, config: MemoryConfig, scope: str) -> None:
        self.store = store
        self.config = config
        self.scope = scope

    def recall(self, goal: str) -> list[str]:
        wanted = keywords(goal)
        scored = []
        for note in self.store.notes(self.scope):
            overlap = len(wanted & set(note["keywords"].split()))
            if overlap:
                scored.append((overlap, note["id"], note["text"]))
        scored.sort(reverse=True)
        return [text for _, _, text in scored[: self.config.max_notes]]

    def record(self, task_id: str, goal: str, result: TaskResult) -> None:
        if result.status == "cancelled":
            return
        failed = [v["detail"] for v in result.verification if not v.get("passed")]
        if result.ok:
            text = (
                f"Task '{goal[:160]}' succeeded. Answer: {(result.answer or '')[:200]}"
            )
        else:
            reason = "; ".join(failed)[:300] or (result.error or "")[:300]
            text = f"Task '{goal[:160]}' failed: {reason}"
        self.store.add_note(self.scope, task_id, text, " ".join(sorted(keywords(goal))))


def build_memory(store: Store, config: MemoryConfig, scope: str) -> MemoryStrategy:
    if config.strategy == "episodic":
        return EpisodicMemory(store, config, scope)
    return NoMemory()


__all__ = ["EpisodicMemory", "MemoryStrategy", "NoMemory", "build_memory", "keywords"]
