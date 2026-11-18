"""内置工作区工具：列目录、读、搜索、整文件写入、精确替换。"""

from __future__ import annotations

import asyncio
from typing import Any

from minimal_local_agent.config import ToolsConfig
from minimal_local_agent.runtime.context import ToolContext
from minimal_local_agent.tools import files
from minimal_local_agent.tools.files import FileChangeSet
from minimal_local_agent.tools.registry import EffectState, Prepared, Tool, schema

FILES = "files"


class FilesEffect:
    """文件变更集的落地、对账与补偿；只依赖账本里的 JSON 数据。"""

    async def apply(self, ctx: ToolContext, data: dict[str, Any]) -> Any:
        change_set = FileChangeSet.from_dict(data)
        # 写盘不受超时取消：一旦开始就让它完成，避免留下半写状态。
        await asyncio.to_thread(files.apply, ctx.workspace, change_set)
        return {"applied": True, **change_set.summary()}

    def probe(self, ctx: ToolContext, data: dict[str, Any]) -> EffectState:
        return files.probe(ctx.workspace, FileChangeSet.from_dict(data))

    def compensate(self, ctx: ToolContext, data: dict[str, Any]) -> None:
        files.compensate(ctx.workspace, FileChangeSet.from_dict(data))


def workspace_tools(config: ToolsConfig) -> list[Tool]:
    async def list_files(ctx: ToolContext, args: dict[str, Any]) -> Any:
        return await asyncio.to_thread(
            ctx.workspace.list_files, args.get("path", "."), args.get("pattern", "**/*")
        )

    async def read_file(ctx: ToolContext, args: dict[str, Any]) -> Any:
        text = await asyncio.to_thread(ctx.workspace.read_file, args["path"])
        return {"path": args["path"], "content": text}

    async def search_text(ctx: ToolContext, args: dict[str, Any]) -> Any:
        return await asyncio.to_thread(
            ctx.workspace.search_text,
            args["query"],
            args.get("path", "."),
            args.get("pattern", "**/*"),
        )

    async def write_file(ctx: ToolContext, args: dict[str, Any]) -> Prepared:
        change_set = await asyncio.to_thread(
            files.prepare_write,
            ctx.workspace,
            args["path"],
            args["content"],
            overwrite=bool(args.get("overwrite", False)),
            max_files=config.max_change_files,
        )
        return Prepared(
            FILES, change_set.to_dict(), change_set.diff(), change_set.summary()
        )

    async def edit_files(ctx: ToolContext, args: dict[str, Any]) -> Prepared:
        change_set = await asyncio.to_thread(
            files.prepare_edits,
            ctx.workspace,
            list(args["edits"]),
            max_files=config.max_change_files,
        )
        return Prepared(
            FILES, change_set.to_dict(), change_set.diff(), change_set.summary()
        )

    path = {"type": "string", "description": "Relative path inside the workspace"}
    pattern = {"type": "string", "description": "Glob pattern, default **/*"}
    return [
        Tool(
            "list_files",
            "List files under a workspace directory.",
            schema({"path": path, "pattern": pattern}, []),
            "read",
            run=list_files,
        ),
        Tool(
            "read_file",
            "Read one UTF-8 text file.",
            schema({"path": path}, ["path"]),
            "read",
            run=read_file,
        ),
        Tool(
            "search_text",
            "Find lines containing a literal string (case-insensitive).",
            schema(
                {"query": {"type": "string"}, "path": path, "pattern": pattern},
                ["query"],
            ),
            "read",
            run=search_text,
        ),
        Tool(
            "write_file",
            "Create a file, or replace a whole file when overwrite is true.",
            schema(
                {
                    "path": path,
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                ["path", "content"],
            ),
            "write",
            prepare=write_file,
        ),
        Tool(
            "edit_files",
            "Apply exact replacements across one or more existing files as one change. "
            "Each old_text must occur exactly once in its file.",
            schema(
                {
                    "edits": {
                        "type": "array",
                        "items": schema(
                            {
                                "path": path,
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                            },
                            ["path", "old_text", "new_text"],
                        ),
                    }
                },
                ["edits"],
            ),
            "write",
            prepare=edit_files,
        ),
    ]


__all__ = ["FILES", "FilesEffect", "workspace_tools"]
