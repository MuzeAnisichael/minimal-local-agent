"""评测用的 MCP 服务器：一个键值存储，数据写在参数给出的 JSON 文件里。

put 是按键覆盖写入，重复执行结果相同，所以评测配置里把它声明为幂等（idempotent）。
"""

import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

STORE = Path(sys.argv[1])
server = FastMCP("kv", log_level="WARNING")


def _load() -> dict[str, str]:
    return json.loads(STORE.read_text("utf-8")) if STORE.exists() else {}


@server.tool()
def put(key: str, value: str) -> str:
    """Set key to value (overwrites; safe to repeat)."""
    data = _load()
    data[key] = value
    STORE.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), "utf-8")
    return f"{key} saved"


@server.tool()
def get(key: str) -> str | None:
    """Return the value of key, or null when missing."""
    return _load().get(key)


if __name__ == "__main__":
    server.run("stdio")
