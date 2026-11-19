"""脚本模型：按预先写好的回合依次给出工具调用或最终回答，不需要真实模型服务。

用于测试和离线评测。它衡量的是运行时本身（幂等、恢复、补偿是否正确），不是模型能力。

脚本格式：
    {"plan": {...可选的计划 JSON...},
     "turns": [{"tool": "read_file", "args": {"path": "a.txt"}},
               {"tools": [{"tool": ..., "args": ...}, ...]},
               {"text": "最终回答"}],
     "variants": [{"when": "某段文字", "turns": [...]}],   # 可选
     "subtask": {...子任务用的脚本...}}                      # 可选

- variants：对话里（包括开头提示里召回的记忆）出现 when 文字时，改用这组回合。
  用来模拟“模型看到了相关经验，就走捷径”，让离线评测能体现记忆策略的作用。
- subtask：带工具但不含 run_subtask 的请求来自子任务（子任务不能再派生子任务），
  交给这份脚本播放。

当前回合号记在每个响应的 provider_response_id 里，所以上下文压缩、检查点恢复之后仍能接着播放。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from minimal_local_agent.core.context import estimate_tokens

DEFAULT_PLAN = {
    "steps": [{"id": "s1", "title": "Complete the task", "done_when": "checks pass"}]
}


def _position(messages: list[ModelMessage]) -> int:
    for message in reversed(messages):
        if isinstance(message, ModelResponse) and (
            message.provider_response_id or ""
        ).startswith("script-"):
            return int(message.provider_response_id.split("-", 1)[1]) + 1
    return 0


def _text_of(messages: list[ModelMessage]) -> str:
    return json.dumps(list(messages), default=str, ensure_ascii=False)


def _turns(
    script: dict[str, Any], messages: list[ModelMessage]
) -> list[dict[str, Any]]:
    variants = script.get("variants") or []
    if variants:
        text = _text_of(messages)
        for variant in variants:
            if variant["when"] in text:
                return list(variant["turns"])
    return list(script.get("turns", []))


def scripted_model(script: dict[str, Any], name: str = "scripted") -> FunctionModel:
    child = script.get("subtask")

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        usage_in = estimate_tokens(messages)
        names = [t.name for t in info.function_tools]
        active = (
            child
            if child is not None and names and "run_subtask" not in names
            else script
        )
        if not info.function_tools:  # 规划或摘要请求
            text = (
                json.dumps(active.get("plan", DEFAULT_PLAN))
                if _is_plan(messages)
                else "summary"
            )
            return ModelResponse(
                parts=[TextPart(text)],
                usage=RequestUsage(
                    input_tokens=usage_in, output_tokens=len(text) // 4 + 1
                ),
                model_name=name,
            )
        index = _position(messages)
        turns = _turns(active, messages)
        turn = turns[index] if index < len(turns) else {"text": "done"}
        if "text" in turn:
            parts: list[Any] = [TextPart(turn["text"])]
        else:
            calls = turn.get("tools") or [turn]
            parts = [
                ToolCallPart(
                    tool_name=c["tool"],
                    args=c["args"]
                    if isinstance(c.get("args"), str)
                    else json.dumps(c.get("args", {})),
                    tool_call_id=f"call-{index}-{n}",
                )
                for n, c in enumerate(calls)
            ]
        output = sum(
            len(str(getattr(p, "content", None) or getattr(p, "args", "")))
            for p in parts
        )
        return ModelResponse(
            parts=parts,
            usage=RequestUsage(input_tokens=usage_in, output_tokens=output // 4 + 1),
            model_name=name,
            provider_response_id=f"script-{index}",
        )

    return FunctionModel(respond, model_name=name)


def _is_plan(messages: list[ModelMessage]) -> bool:
    return "planning a task" in _text_of(messages)


__all__ = ["DEFAULT_PLAN", "scripted_model"]
