"""上下文预算：按 token 估算消息大小，超出时以“步骤”为单位压缩历史。

消息结构固定为：[开头请求(系统提示 + 目标 + 计划), 响应1, 请求1(工具结果), 响应2, ...]。
压缩时保留开头请求和最近 K 个步骤，中间的步骤整体丢弃（工具调用与结果成对丢弃，
不会留下孤立的 tool_call），并把一段摘要追加到开头请求里。
完整历史不会丢失：每次模型调用和工具结果都已写入事件表。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)

MARKER = "[Context compacted:"
PLAN_MARKER = "Current plan:"


class ContextOverflow(RuntimeError):
    pass


def estimate_tokens(messages: list[ModelMessage]) -> int:
    """不依赖分词器的保守估算：ASCII 约 4 字符 1 token，其他字符（如中文）按 1 字符 1 token。"""

    text = ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return ascii_chars // 4 + (len(text) - ascii_chars) + 1


def _units(messages: list[ModelMessage]) -> list[list[ModelMessage]]:
    """把开头请求之后的消息切成步骤：每个步骤以一个 ModelResponse 开头。"""

    units: list[list[ModelMessage]] = []
    for message in messages[1:]:
        if isinstance(message, ModelResponse) or not units:
            units.append([message])
        else:
            units[-1].append(message)
    return units


def digest_units(units: list[list[ModelMessage]]) -> str:
    """不调用模型的摘要：列出被省略步骤里调用过的工具和参数要点。"""

    lines: list[str] = []
    for unit in units:
        for message in unit:
            if isinstance(message, ModelResponse):
                for part in message.parts:
                    if isinstance(part, ToolCallPart):
                        args = part.args_as_json_str()
                        lines.append(f"- called {part.tool_name}({args[:160]})")
    return "\n".join(lines[-40:]) or "- (no tool calls)"


Summarizer = Callable[[list[list[ModelMessage]]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class Compaction:
    messages: list[ModelMessage]
    before_tokens: int
    after_tokens: int
    dropped_steps: int


async def fit(
    messages: list[ModelMessage],
    max_tokens: int,
    keep_recent: int,
    *,
    summarize: Summarizer | None = None,
    extra_note: str = "",
) -> Compaction | None:
    """不超预算时返回 None；否则返回压缩后的消息，仍然放不下则抛 ContextOverflow。"""

    before = estimate_tokens(messages)
    if before <= max_tokens:
        return None
    units = _units(messages)
    head = messages[0]
    assert isinstance(head, ModelRequest)
    parts = list(head.parts)
    previous = ""
    # 之前压缩留下的摘要合并进新摘要，开头请求里始终只保留一段摘要。
    if (
        parts
        and isinstance(parts[-1], UserPromptPart)
        and str(parts[-1].content).startswith(MARKER)
    ):
        previous = str(parts.pop().content).split("\n", 2)[-1]
        previous = previous.split(PLAN_MARKER)[0].strip()
    for keep in range(min(keep_recent, len(units)), 0, -1):
        dropped, kept = units[:-keep], units[-keep:]
        if not dropped:
            continue
        summary = await summarize(dropped) if summarize else digest_units(dropped)
        note = f"{MARKER} {len(dropped)} more steps were removed.]\nSummary of removed steps:\n"
        note += f"{previous}\n{summary}".strip()
        if extra_note:
            note += f"\n{PLAN_MARKER}\n{extra_note}"
        new_head = ModelRequest(parts=[*parts, UserPromptPart(content=note)])
        candidate: list[ModelMessage] = [new_head, *(m for unit in kept for m in unit)]
        after = estimate_tokens(candidate)
        if after <= max_tokens:
            return Compaction(candidate, before, after, len(dropped))
    raise ContextOverflow(
        f"Context needs about {before} tokens; limit is {max_tokens} even after compaction"
    )


def serialize(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    return ModelMessagesTypeAdapter.dump_python(messages, mode="json")


def deserialize(data: list[dict[str, Any]]) -> list[ModelMessage]:
    return ModelMessagesTypeAdapter.validate_python(data)


__all__ = [
    "Compaction",
    "ContextOverflow",
    "Summarizer",
    "deserialize",
    "digest_units",
    "estimate_tokens",
    "fit",
    "serialize",
]
