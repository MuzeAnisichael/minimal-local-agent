"""测试用的 MCP 服务器：笔记写入一个 JSON 文件（路径由 NOTES_FILE 环境变量或参数给出）。"""

import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

STORE = Path(sys.argv[1])
server = FastMCP("notes")


def _load() -> list[str]:
    return json.loads(STORE.read_text()) if STORE.exists() else []


@server.tool()
def add_note(text: str) -> str:
    """Append a note."""
    notes = _load()
    notes.append(text)
    STORE.write_text(json.dumps(notes))
    return f"saved {len(notes)}"


@server.tool()
def search_notes(query: str) -> list[str]:
    """Find notes containing the query."""
    return [n for n in _load() if query.lower() in n.lower()]


if __name__ == "__main__":
    server.run("stdio")
