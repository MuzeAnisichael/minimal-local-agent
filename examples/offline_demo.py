"""离线演示：不需要任何模型服务，几秒内看到检查点、崩溃续跑和产物回溯。

用法（在仓库根目录）：

    python examples/offline_demo.py

演示里的“模型”是脚本模型（按写好的回合依次给出工具调用），所以结果是确定的；
它展示的是运行时机制，不代表模型能力。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from minimal_local_agent.config import ModelConfig, Settings, WorkspaceConfig
from minimal_local_agent.eval import faults
from minimal_local_agent.model.scripted import scripted_model
from minimal_local_agent.runtime import trace
from minimal_local_agent.runtime.runtime import AgentRuntime

GOAL = "把 config.ini 里的 debug 关掉"
CHECKS = [{"type": "file_equals", "path": "config.ini", "content": "debug = false\n"}]
SCRIPT = {
    "turns": [
        {"tool": "read_file", "args": {"path": "config.ini"}},
        {
            "tool": "edit_files",
            "args": {
                "edits": [
                    {
                        "path": "config.ini",
                        "old_text": "debug = true",
                        "new_text": "debug = false",
                    }
                ]
            },
        },
        {"text": "已关闭 debug"},
    ]
}


def make_runtime(root: Path) -> AgentRuntime:
    settings = Settings(
        workspace=WorkspaceConfig(root=root / "ws", state_dir=root / "state"),
        models={"default": ModelConfig(provider="test")},
    )
    return AgentRuntime(
        settings,
        models={"default": scripted_model(SCRIPT)},
        approver=lambda _tool, _preview: True,  # 演示里自动批准写入
    )


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="mla-demo-"))
    (root / "ws").mkdir()
    (root / "ws" / "config.ini").write_text("debug = true\n", encoding="utf-8")

    section("1. 写盘后、账本标记完成前进程崩溃")
    rt = make_runtime(root)
    faults.crash_during_write(rt)
    state = rt.new_task(GOAL, checks=CHECKS)
    try:
        rt._sync(rt._drive(state))
    except faults.SimulatedCrash:
        print(f"任务 {state.task_id} 崩溃，账本里这次写入停在 applying")

    section("2. 全新进程从检查点续跑")
    result = make_runtime(root).resume(state.task_id)
    content = (root / "ws" / "config.ini").read_text(encoding="utf-8")
    print(f"状态：{result.status}  回答：{result.answer}")
    print(f"config.ini = {content!r}（对账判断已写入，没有重复写）")

    section("3. 事件树（mla trace TASK_ID）")
    store = make_runtime(root).store
    print("\n".join(trace.tree(store, state.task_id)))

    section("4. 从产物回溯来源（mla trace --artifact config.ini）")
    for event in trace.artifact(store, path="config.ini"):
        print(f"#{event['id']} {event['kind']}  {trace.brief(event)}")

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
