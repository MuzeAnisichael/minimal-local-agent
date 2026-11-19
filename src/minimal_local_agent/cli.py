"""命令行：run / resume / cancel / tasks / show / trace / undo / eval。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from minimal_local_agent.config import Settings


def _approver(assume_yes: bool):
    def approve(tool: str, preview: str) -> bool:
        if assume_yes:
            return True
        print(f"\n[{tool}] 请求修改文件：\n{preview}\n", file=sys.stderr)
        try:
            return input("批准这次修改？[y/N] ").strip().lower() in {"y", "yes"}
        except EOFError:
            return False

    return approve


def _printer(event: dict[str, Any]) -> None:
    from minimal_local_agent.runtime.trace import brief

    data = {k: v for k, v in event.items() if k not in {"id", "task_id", "kind"}}
    print(
        f"  · {event['kind']} {brief({'kind': event['kind'], 'data': data})}",
        file=sys.stderr,
    )


def _runtime(settings: Settings, args: argparse.Namespace):
    from minimal_local_agent.runtime.runtime import AgentRuntime

    return AgentRuntime(
        settings,
        approver=_approver(getattr(args, "yes", False)),
        on_event=_printer if getattr(args, "events", False) else None,
    )


def _emit(result: Any, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"任务 {result.task_id}：{result.status}")
        if result.answer:
            print(result.answer)
        if result.error:
            print(f"错误：{result.error}", file=sys.stderr)
        failed = [v for v in result.verification if not v.get("passed")]
        for item in failed:
            print(f"未通过校验：{item['detail']}", file=sys.stderr)
        usage = result.usage
        print(
            f"用量：请求 {usage.get('requests')}，工具 {usage.get('tool_calls')}，"
            f"输出 token {usage.get('output_tokens')}，成本 {usage.get('cost_usd')}",
            file=sys.stderr,
        )
    return 0 if result.status == "completed" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mla", description="有状态 Agent 执行内核")
    parser.add_argument("--config", help="配置文件路径，默认 ./agent.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行一个任务")
    run.add_argument("goal")
    run.add_argument("--checks", help="校验规则 JSON 文件（列表）")
    run.add_argument("--yes", action="store_true", help="自动批准文件修改")
    run.add_argument("--events", action="store_true", help="实时打印事件")
    run.add_argument("--json", action="store_true")

    resume = sub.add_parser("resume", help="从最近的检查点继续一个中断的任务")
    resume.add_argument("task_id")
    resume.add_argument("--yes", action="store_true")
    resume.add_argument("--events", action="store_true")
    resume.add_argument("--json", action="store_true")

    cancel = sub.add_parser(
        "cancel", help="请求取消任务（对另一个进程中运行的任务也有效）"
    )
    cancel.add_argument("task_id")

    tasks = sub.add_parser("tasks", help="列出最近的任务")
    tasks.add_argument("--limit", type=int, default=20)

    show = sub.add_parser("show", help="显示任务结果与工具账本")
    show.add_argument("task_id")

    trace = sub.add_parser("trace", help="打印事件树，或从产物回溯来源")
    trace.add_argument("task_id", nargs="?")
    trace.add_argument("--artifact", help="工作区里的文件路径")

    undo = sub.add_parser(
        "undo", help="补偿：撤销任务写入的文件（被外部改过的文件不会覆盖）"
    )
    undo.add_argument("task_id")

    ev = sub.add_parser("eval", help="按任务族评测，并对比策略")
    ev.add_argument("--families", default="evals/families", help="任务族 JSON 目录")
    ev.add_argument("--offline", action="store_true", help="使用用例自带的脚本模型")
    ev.add_argument("--strategies", default="baseline,memory,recovery,full")
    ev.add_argument("--no-faults", action="store_true", help="不注入故障")
    ev.add_argument("--repeat", type=int, default=1)
    ev.add_argument("--out", help="把完整报告写成 JSON")
    ev.add_argument(
        "--model", help="在线评测时固定使用配置里的这个模型（所有角色都用它）"
    )
    ev.add_argument("--only", help="只跑这些任务族，逗号分隔")
    return parser


def _utf8_output() -> None:
    """输出里有中文；Windows 控制台或重定向时默认编码可能是 cp1252，统一改成 UTF-8。"""

    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_output()
    args = _parser().parse_args(argv)
    settings = Settings.load(args.config)

    if args.command == "eval":
        from minimal_local_agent.eval.runner import main as eval_main

        return eval_main(settings, args)

    from minimal_local_agent.runtime import trace
    from minimal_local_agent.runtime.store import Store

    if args.command == "run":
        checks = (
            json.loads(Path(args.checks).read_text("utf-8")) if args.checks else None
        )
        return _emit(_runtime(settings, args).run(args.goal, checks=checks), args.json)
    if args.command == "resume":
        return _emit(_runtime(settings, args).resume(args.task_id), args.json)

    store = Store(settings.database)
    if args.command == "cancel":
        ok = store.request_cancel(args.task_id)
        print("已请求取消" if ok else "任务不存在或已结束")
        return 0 if ok else 1
    if args.command == "tasks":
        for task in store.tasks(args.limit):
            print(
                f"{task['id']}  {task['status']:<10} {task['created_at'][:19]}  {task['goal'][:60]}"
            )
        return 0
    if args.command == "show":
        task = store.task(args.task_id)
        if task is None:
            print("任务不存在", file=sys.stderr)
            return 1
        print(json.dumps(task, ensure_ascii=False, indent=2, default=str))
        for row in store.ledger_for_task(args.task_id):
            print(f"  账本 {row['key'][:12]} {row['tool']:<12} {row['status']}")
        return 0
    if args.command == "trace":
        if args.artifact:
            chain = trace.artifact(store, path=args.artifact)
            if not chain:
                print("没有找到写出该文件的事件", file=sys.stderr)
                return 1
            for event in chain:
                print(f"#{event['id']} {event['kind']}  {trace.brief(event)}")
            return 0
        if not args.task_id:
            print("需要 task_id 或 --artifact", file=sys.stderr)
            return 2
        print("\n".join(trace.tree(store, args.task_id)))
        return 0
    if args.command == "undo":
        rt = _runtime(settings, args)
        rt.settings = rt.settings.model_copy(
            update={
                "recovery": rt.settings.recovery.model_copy(
                    update={"enabled": True, "compensate_on_failure": True}
                )
            }
        )
        outcomes = rt.compensate(args.task_id)
        for item in outcomes:
            print(
                f"{item['tool']}: {item['status']} {', '.join(item.get('paths', []))}"
            )
        return 0 if all(o["status"] == "compensated" for o in outcomes) else 1
    return 2


__all__ = ["main"]
