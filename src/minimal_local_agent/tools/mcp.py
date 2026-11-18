"""MCP 接入：把 MCP 服务器上白名单里的工具转换成统一的 Tool（直接使用官方 mcp SDK）。

- 支持 stdio（command）和 Streamable HTTP（url）两种传输；
- 只暴露配置里列出的工具，并按配置标注副作用类型（read / write）；
- 写工具经账本执行。外部系统的状态无法对账（probe 返回 unknown），
  所以中断后只有声明为 idempotent 的写工具会被重放，其余报告“结果未知”；
- 外部写操作不支持自动补偿，补偿时记为 compensation.skipped。

连接器对象在运行时里长期存在；每次 asyncio.run 结束时断开会话，下次使用时重新连接，
工具和 effect 始终通过服务器名找到当前会话。
"""

from __future__ import annotations

import contextlib
import json
from contextlib import AsyncExitStack
from typing import Any

from minimal_local_agent.config import MCPConfig, MCPServerConfig, ToolsConfig
from minimal_local_agent.runtime.context import ToolContext
from minimal_local_agent.tools.registry import (
    EffectHandler,
    EffectState,
    Prepared,
    Tool,
)


def _content(result: Any) -> Any:
    """把 CallToolResult 转成可 JSON 序列化的值；服务器报错时抛 RuntimeError。"""

    texts = [getattr(item, "text", None) or str(item) for item in result.content or []]
    if result.isError:
        raise RuntimeError("; ".join(texts) or "MCP tool reported an error")
    if result.structuredContent is not None:
        return result.structuredContent
    return texts[0] if len(texts) == 1 else texts


class MCPConnector:
    def __init__(self, config: MCPConfig, tools: ToolsConfig) -> None:
        self.config = config
        self.tools = tools
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, Any] = {}
        self._catalog: dict[str, dict[str, Any]] = {}

    async def _open(self, cfg: MCPServerConfig, stack: AsyncExitStack) -> Any:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:  # pragma: no cover - 取决于是否安装 mcp 可选依赖
            raise RuntimeError(
                'MCP is configured; install it with: pip install ".[mcp]"'
            ) from exc
        if cfg.command:
            params = StdioServerParameters(command=cfg.command[0], args=cfg.command[1:])
            read, write = await stack.enter_async_context(stdio_client(params))
        else:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(cfg.url)
            )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def connect(self) -> None:
        if self._stack is not None:
            return
        stack = AsyncExitStack()
        try:
            for cfg in self.config.servers:
                session = await self._open(cfg, stack)
                self._sessions[cfg.name] = session
                listed = {t.name: t for t in (await session.list_tools()).tools}
                missing = sorted(set(cfg.tools) - set(listed))
                if missing:
                    raise RuntimeError(
                        f"MCP server {cfg.name} does not offer: {missing}"
                    )
                self._catalog[cfg.name] = listed
        except BaseException:
            await stack.aclose()
            self._sessions.clear()
            raise
        self._stack = stack

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        session = self._sessions.get(server)
        if session is None:
            raise RuntimeError(f"MCP server {server} is not connected")
        return _content(await session.call_tool(tool, args))

    def build(self) -> tuple[list[Tool], dict[str, EffectHandler]]:
        tools: list[Tool] = []
        effects: dict[str, EffectHandler] = {}
        for cfg in self.config.servers:
            kind = f"mcp:{cfg.name}"
            effects[kind] = MCPEffect(self, cfg.name)
            for name, effect in cfg.tools.items():
                tools.append(
                    self._tool(cfg, kind, self._catalog[cfg.name][name], effect)
                )
        return tools, effects

    def _tool(self, cfg: MCPServerConfig, kind: str, remote: Any, effect: str) -> Tool:
        visible = f"mcp_{cfg.name}_{remote.name}"
        parameters = dict(remote.inputSchema or {"type": "object", "properties": {}})
        description = f"[MCP {cfg.name}] {remote.description or remote.name}"
        server, name = cfg.name, remote.name

        if effect == "read":

            async def run(ctx: ToolContext, args: dict[str, Any]) -> Any:
                return await self.call(server, name, args)

            return Tool(
                visible,
                description,
                parameters,
                "read",
                run=run,
                timeout_s=cfg.timeout_s,
            )

        async def prepare(ctx: ToolContext, args: dict[str, Any]) -> Prepared:
            preview = f"{visible}({json.dumps(args, ensure_ascii=False)})"
            return Prepared(
                kind, {"tool": name, "args": args}, preview, {"mcp": visible}
            )

        return Tool(
            visible,
            description,
            parameters,
            "write",
            prepare=prepare,
            timeout_s=cfg.timeout_s,
            replay_safe=name in cfg.idempotent,
        )

    async def close(self) -> None:
        stack, self._stack = self._stack, None
        self._sessions.clear()
        if stack is not None:
            with contextlib.suppress(Exception):  # 关闭失败不影响任务结果
                await stack.aclose()


class MCPEffect:
    def __init__(self, connector: MCPConnector, server: str) -> None:
        self.connector = connector
        self.server = server

    async def apply(self, ctx: ToolContext, data: dict[str, Any]) -> Any:
        await self.connector.connect()
        result = await self.connector.call(self.server, data["tool"], data["args"])
        return {"applied": True, "result": result}

    def probe(self, ctx: ToolContext, data: dict[str, Any]) -> EffectState:
        return "unknown"

    def compensate(self, ctx: ToolContext, data: dict[str, Any]) -> None:
        raise NotImplementedError("External MCP writes cannot be compensated")


__all__ = ["MCPConnector", "MCPEffect"]
