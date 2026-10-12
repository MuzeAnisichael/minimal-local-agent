"""One run budget, including optional planning and explicit peer reservations."""

from __future__ import annotations

from threading import Lock
from typing import Any


class ExecutionBudget:
    def __init__(self, requests: int, tools: int, output_tokens: int) -> None:
        self.requests, self.tools, self.output_tokens = requests, tools, output_tokens
        self.extra_requests = self.extra_tools = self.extra_output = 0
        self.planning_usage: dict[str, Any] = {}
        self.planning_allocated = False
        self.peer_usage: list[dict[str, Any]] = []
        self.peer_allocations = 0
        self._active_tools = 0
        self._lock = Lock()

    @staticmethod
    def _count(usage: dict[str, Any], name: str, fallback: int) -> int:
        value = usage.get(name)
        if value is None:
            return fallback
        if type(value) is not int or value < 0:
            raise ValueError(f"Invalid extension usage: {name}")
        return value

    def planning(self, usage: dict[str, Any], reserved_output: int) -> None:
        requests = self._count(usage, "requests", 1)
        output = self._count(usage, "output_tokens", 0)
        if requests > 1 or output > reserved_output:
            raise ValueError("Planner exceeded its allocated budget")
        self.planning_usage = dict(usage)
        self.planning_allocated = True
        self.extra_requests += requests
        # Unreported tokens are unknown, not free. Reserve the allocated maximum.
        self.extra_output += output or (reserved_output if requests else 0)

    def before_model(self, usage: Any) -> int:
        with self._lock:
            if usage.requests + self.extra_requests >= self.requests:
                raise ValueError("Shared model request budget exhausted")
            remaining = self.output_tokens - usage.output_tokens - self.extra_output
            if remaining < 1:
                raise ValueError("Shared output token budget exhausted")
            if usage.tool_calls + self.extra_tools > self.tools:
                raise ValueError("Shared tool-call budget exceeded")
            return remaining

    def reserve_peer(self, usage: Any) -> tuple[int, int, int]:
        with self._lock:
            # Keep a model request for consuming the returned task result.
            if usage.requests + self.extra_requests + 2 > self.requests:
                raise ValueError("No shared request budget remains for delegation")
            if (
                usage.tool_calls + self.extra_tools + self._active_tools + 1
                > self.tools
            ):
                raise ValueError("No shared tool budget remains for delegation")
            remaining = self.output_tokens - usage.output_tokens - self.extra_output
            if remaining < 2:
                raise ValueError("No shared output budget remains for delegation")
            output = min(512, max(1, remaining // 2))
            self.extra_requests += 1
            self.extra_tools += 1
            self.extra_output += output
            self.peer_allocations += 1
            return 1, 1, output

    def after_model(self, usage: Any, response_usage: Any) -> None:
        with self._lock:
            if (
                usage.output_tokens + response_usage.output_tokens + self.extra_output
                > self.output_tokens
            ):
                raise ValueError(
                    "Shared output token budget exceeded by model response"
                )

    def tool_started(self, usage: Any) -> None:
        with self._lock:
            if usage.tool_calls + self.extra_tools + self._active_tools >= self.tools:
                raise ValueError("Shared tool-call budget exhausted")
            self._active_tools += 1

    def tool_finished(self) -> None:
        with self._lock:
            self._active_tools -= 1

    def peer_completed(
        self, usage: dict[str, Any], allocation: tuple[int, int, int]
    ) -> None:
        requests, tools, output = allocation
        used_requests = self._count(usage, "requests", requests)
        used_tools = self._count(usage, "tool_calls", tools)
        used_output = self._count(usage, "output_tokens", 0)
        if used_requests > requests or used_tools > tools or used_output > output:
            raise ValueError("Peer reported usage beyond its allocated budget")
        with self._lock:
            self.extra_requests -= requests - used_requests
            self.extra_tools -= tools - used_tools
            if used_output:
                self.extra_output -= output - used_output
            self.peer_usage.append(dict(usage))

    def summary(self, main: dict[str, Any]) -> dict[str, Any]:
        if (
            self._count(main, "output_tokens", 0) + self.extra_output
            > self.output_tokens
        ):
            raise ValueError("Shared output token budget exceeded")
        if not self.planning_allocated and not self.peer_allocations:
            return main
        result = dict(main)
        additions = [
            *([self.planning_usage] if self.planning_allocated else []),
            *self.peer_usage,
        ]
        for key in ("requests", "tool_calls", "input_tokens", "output_tokens"):
            if key in main:
                incomplete = any(item.get(key) is None for item in additions)
                if self.peer_allocations != len(self.peer_usage) or any(
                    not item for item in self.peer_usage
                ):
                    incomplete = True
                result[key] = (
                    None
                    if incomplete
                    else main[key]
                    + sum(self._count(item, key, 0) for item in additions)
                )
        result.update(
            main_usage=main,
            planning_usage=self.planning_usage,
            peer_usage=self.peer_usage,
            peer_usage_complete=(
                self.peer_allocations == len(self.peer_usage)
                and all(
                    all(
                        item.get(key) is not None
                        for key in (
                            "requests",
                            "tool_calls",
                            "input_tokens",
                            "output_tokens",
                        )
                    )
                    for item in self.peer_usage
                )
            ),
            reserved_extension_requests=self.extra_requests,
            reserved_extension_tool_calls=self.extra_tools,
            reserved_extension_output_tokens=self.extra_output,
        )
        return result
