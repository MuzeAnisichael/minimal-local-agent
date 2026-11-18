"""按任务族评测：在固定模型与调用预算下，对比基础循环、记忆与恢复策略。

数据集是 `evals/families/*.json`，每个文件一个任务族：

    {"family": "single_edit", "title": "单文件修改",
     "settings": {...对本族所有用例生效的配置覆盖...},
     "cases": [{"id": "...", "goal": "...", "files": {"a.txt": "..."},
                "checks": [...], "script": {...离线脚本...},
                "fault": {"type": "crash_during_write", ...},   # 可选
                "settings": {...}, "budget": {...}}]}

策略只是配置覆盖（见 STRATEGIES）。同一族的用例按顺序在同一个状态库里运行，
所以记忆策略能用上前面用例留下的经验；每个用例开始前重置工作区文件。

指标：
- 成功率：通过全部校验的用例 / 用例数；
- 故障恢复率：注入了故障的用例里，最终成功的比例；
- 每成功任务成本：所有用例（包括失败的）花费之和 / 成功用例数，tokens 同理。

离线模式（--offline）使用用例自带的脚本模型，衡量的是运行时机制本身
（崩溃后续跑、对账、重试、重新规划、记忆注入），结果确定、可在 CI 里跑；
在线模式使用配置里的真实模型，忽略脚本，才反映模型能力。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minimal_local_agent.config import Settings
from minimal_local_agent.eval import faults

# 离线模式的名义价格（美元 / 百万 tokens），只用于让“每成功任务成本”可比。
OFFLINE_PRICES = {"input_price_per_mtok": 1.0, "output_price_per_mtok": 4.0}

STRATEGIES: dict[str, dict[str, Any]] = {
    "baseline": {
        "recovery": {"enabled": False},
        "planning": {"max_replans": 0},
        "memory": {"strategy": "none"},
    },
    "memory": {
        "recovery": {"enabled": False},
        "planning": {"max_replans": 0},
        "memory": {"strategy": "episodic"},
    },
    "recovery": {
        "recovery": {"enabled": True},
        "planning": {"max_replans": 2},
        "memory": {"strategy": "none"},
    },
    "full": {
        "recovery": {"enabled": True},
        "planning": {"max_replans": 2},
        "memory": {"strategy": "episodic"},
    },
}

STRATEGY_NAMES = {
    "baseline": "基础循环",
    "memory": "记忆",
    "recovery": "恢复",
    "full": "记忆+恢复",
}


def merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def _substitute(value: Any, names: dict[str, str]) -> Any:
    """把配置里的 {python}、{workspace}、{families} 等占位符换成本次运行的实际路径。"""

    if isinstance(value, str):
        for key, real in names.items():
            value = value.replace("{" + key + "}", real)
        return value
    if isinstance(value, list):
        return [_substitute(v, names) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, names) for k, v in value.items()}
    return value


def load_families(directory: str | Path) -> list[dict[str, Any]]:
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"Eval families not found: {root}")
    families = []
    for path in sorted(root.glob("*.json")):
        data = json.loads(path.read_text("utf-8"))
        data.setdefault("family", path.stem)
        data["_dir"] = str(root.resolve())
        families.append(data)
    return families


def _needs_mcp(family: dict[str, Any]) -> bool:
    if (family.get("settings") or {}).get("mcp"):
        return True
    return any((c.get("settings") or {}).get("mcp") for c in family["cases"])


@dataclass
class CaseResult:
    family: str
    case: str
    strategy: str
    repeat: int
    status: str
    fault: dict[str, Any] | None
    usage: dict[str, Any]
    crashed: bool = False
    resumed: bool = False
    error: str | None = None
    task_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "case": self.case,
            "strategy": self.strategy,
            "repeat": self.repeat,
            "status": self.status,
            "ok": self.ok,
            "fault": self.fault,
            "crashed": self.crashed,
            "resumed": self.resumed,
            "usage": self.usage,
            "error": self.error,
            "task_id": self.task_id,
        }


@dataclass
class Metrics:
    cases: int = 0
    successes: int = 0
    fault_cases: int = 0
    recovered: int = 0
    cost_usd: float = 0.0
    cost_complete: bool = True
    tokens: int = 0
    requests: int = 0
    tool_calls: int = 0
    failures: list[str] = field(default_factory=list)

    def add(self, r: CaseResult) -> None:
        self.cases += 1
        self.successes += r.ok
        if r.fault is not None:
            self.fault_cases += 1
            self.recovered += r.ok
        if r.usage.get("cost_usd") is None:
            self.cost_complete = False
        else:
            self.cost_usd += r.usage["cost_usd"]
        self.tokens += r.usage.get("input_tokens", 0) + r.usage.get("output_tokens", 0)
        self.requests += r.usage.get("requests", 0)
        self.tool_calls += r.usage.get("tool_calls", 0)
        if not r.ok:
            self.failures.append(r.case)

    def to_dict(self) -> dict[str, Any]:
        per = (lambda v: round(v / self.successes, 8)) if self.successes else None
        return {
            "cases": self.cases,
            "successes": self.successes,
            "success_rate": round(self.successes / self.cases, 4)
            if self.cases
            else None,
            "fault_cases": self.fault_cases,
            "recovered": self.recovered,
            "recovery_rate": (
                round(self.recovered / self.fault_cases, 4)
                if self.fault_cases
                else None
            ),
            "cost_usd": round(self.cost_usd, 8) if self.cost_complete else None,
            "cost_per_success": (
                per(self.cost_usd) if per and self.cost_complete else None
            ),
            "tokens": self.tokens,
            "tokens_per_success": per(self.tokens) if per else None,
            "requests": self.requests,
            "tool_calls": self.tool_calls,
            "failed_cases": self.failures,
        }


class EvalRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        offline: bool = True,
        inject_faults: bool = True,
        workdir: Path | None = None,
        model: str | None = None,
    ) -> None:
        if model is not None and not offline and model not in settings.models:
            raise ValueError(f"Unknown model: {model}")
        self.settings = settings
        self.offline = offline
        self.model = None if offline else model
        self.inject_faults = inject_faults
        self.workdir = workdir

    # ---- 配置 ----
    def _settings(
        self,
        family: dict[str, Any],
        case: dict[str, Any],
        strategy: str,
        run_dir: Path,
    ) -> Settings:
        base = self.settings.model_dump(mode="json")
        data = merge(base, family.get("settings") or {})
        data = merge(data, case.get("settings") or {})
        if case.get("budget"):
            data = merge(data, {"budget": case["budget"]})
        data = merge(data, STRATEGIES[strategy])
        data["workspace"] = {
            "root": str(run_dir / "ws"),
            "state_dir": str(run_dir / "state"),
        }
        if self.offline:
            data["models"] = {
                "default": {"provider": "test", "model": "scripted", **OFFLINE_PRICES}
            }
            data["routing"] = {
                "plan": ["default"],
                "act": ["default"],
                "summarize": ["default"],
            }
        elif self.model:
            # 固定模型：所有角色只用这一个模型，不回退
            data["routing"] = {r: [self.model] for r in ("plan", "act", "summarize")}
        names = {
            "python": sys.executable,
            "workspace": str(run_dir / "ws"),
            "run": str(run_dir),
            "families": family["_dir"],
        }
        return Settings.model_validate(_substitute(data, names))

    def _models(self, settings: Settings, case: dict[str, Any], fault: Any) -> Any:
        from minimal_local_agent.model.adapter import build_model
        from minimal_local_agent.model.scripted import scripted_model

        bad = fault is not None and fault.get("type") == "bad_call"
        if self.offline:
            model = scripted_model(case.get("script") or {})
            return {"default": faults.BadCallModel(model) if bad else model}
        if not bad:
            return None
        routing = settings.routing
        used = {n for r in ("plan", "act", "summarize") for n in getattr(routing, r)}
        return {
            name: faults.BadCallModel(build_model(settings.models[name]))
            for name in sorted(used)
        }

    def _runtime(self, settings: Settings, case: dict[str, Any], fault: Any) -> Any:
        from minimal_local_agent.runtime.runtime import AgentRuntime

        return AgentRuntime(
            settings,
            models=self._models(settings, case, fault),
            approver=lambda _tool, _preview: True,
        )

    @staticmethod
    def _reset_workspace(root: Path, files: dict[str, str]) -> None:
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")

    @staticmethod
    def _install(rt: Any, fault: dict[str, Any]) -> None:
        kind = fault["type"]
        if kind == "bad_call":
            return  # 已经包在模型外面
        if rt.settings.mcp.servers:
            rt._sync(rt._ensure_mcp())  # MCP 工具和 effect 连接后才存在
        args = {k: v for k, v in fault.items() if k != "type"}
        if kind not in faults.FAULTS:
            raise ValueError(f"Unknown fault: {kind}")
        faults.FAULTS[kind](rt, **args)

    # ---- 执行 ----
    def run_case(
        self,
        family: dict[str, Any],
        case: dict[str, Any],
        strategy: str,
        run_dir: Path,
        repeat: int = 0,
    ) -> CaseResult:
        fault = case.get("fault") if self.inject_faults else None
        settings = self._settings(family, case, strategy, run_dir)
        self._reset_workspace(settings.workspace.root, case.get("files") or {})
        rt = self._runtime(settings, case, fault)
        if fault is not None:
            self._install(rt, fault)
        checks = case.get("checks") or []
        crashed = resumed = False
        state = rt.new_task(case["goal"], checks=checks)
        task_id = state.task_id
        try:
            result = rt._sync(rt._drive(state))
        except faults.SimulatedCrash:
            crashed = True
            if not settings.recovery.enabled:
                # 没有恢复机制：进程死了，任务就丢了
                state = rt.load_state(task_id)
                return CaseResult(
                    family["family"],
                    case["id"],
                    strategy,
                    repeat,
                    "crashed",
                    fault,
                    state.budget.used.to_dict(),
                    crashed=True,
                    error="SimulatedCrash: process died, recovery disabled",
                    task_id=task_id,
                )
            # 模拟进程重启：全新的运行时、不再注入故障，从检查点续跑
            fresh = self._runtime(settings, case, None)
            result = fresh.resume(task_id)
            resumed = True
        return CaseResult(
            family["family"],
            case["id"],
            strategy,
            repeat,
            result.status,
            fault,
            result.usage,
            crashed=crashed,
            resumed=resumed,
            error=result.error,
            task_id=result.task_id,
        )

    def _safe_case(
        self,
        family: dict[str, Any],
        case: dict[str, Any],
        strategy: str,
        run_dir: Path,
        repeat: int,
    ) -> CaseResult:
        """评测框架自身出错（例如连不上模型服务）时记为 error，不中断整轮评测。"""

        try:
            return self.run_case(family, case, strategy, run_dir, repeat)
        except Exception as exc:
            if self.offline:
                raise  # 离线结果应当确定，出错说明有 bug
            fault = case.get("fault") if self.inject_faults else None
            return CaseResult(
                family["family"],
                case["id"],
                strategy,
                repeat,
                "error",
                fault,
                {},
                error=f"{type(exc).__name__}: {exc}",
            )

    def run(
        self,
        families: list[dict[str, Any]],
        strategies: list[str],
        repeat: int = 1,
    ) -> dict[str, Any]:
        unknown = [s for s in strategies if s not in STRATEGIES]
        if unknown:
            raise ValueError(f"Unknown strategies: {unknown}")
        results: list[CaseResult] = []
        skipped: dict[str, str] = {}
        owned = self.workdir is None
        workdir = self.workdir or Path(tempfile.mkdtemp(prefix="mla-eval-"))
        try:
            for family in families:
                if _needs_mcp(family) and not _has_mcp():
                    skipped[family["family"]] = (
                        'mcp not installed (pip install ".[mcp]")'
                    )
                    continue
                for strategy in strategies:
                    for n in range(repeat):
                        run_dir = workdir / family["family"] / strategy / str(n)
                        for case in family["cases"]:
                            results.append(
                                self._safe_case(family, case, strategy, run_dir, n)
                            )
        finally:
            if owned:
                shutil.rmtree(workdir, ignore_errors=True)
        return report(results, strategies, families, skipped, self)


def _has_mcp() -> bool:
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


def report(
    results: list[CaseResult],
    strategies: list[str],
    families: list[dict[str, Any]],
    skipped: dict[str, str],
    runner: EvalRunner,
) -> dict[str, Any]:
    by_family: dict[str, dict[str, Metrics]] = {}
    overall = {s: Metrics() for s in strategies}
    for r in results:
        by_family.setdefault(r.family, {s: Metrics() for s in strategies})[
            r.strategy
        ].add(r)
        overall[r.strategy].add(r)
    titles = {f["family"]: f.get("title", f["family"]) for f in families}
    return {
        "mode": "offline" if runner.offline else "online",
        "model": "scripted" if runner.offline else (runner.model or "routing"),
        "faults": runner.inject_faults,
        "strategies": {s: STRATEGIES[s] for s in strategies},
        "budget": runner.settings.budget.model_dump(mode="json"),
        "prices": OFFLINE_PRICES if runner.offline else _prices(runner),
        "families": {
            name: {
                "title": titles.get(name, name),
                "metrics": {s: m.to_dict() for s, m in metrics.items()},
            }
            for name, metrics in by_family.items()
        },
        "overall": {s: m.to_dict() for s, m in overall.items()},
        "skipped": skipped,
        "cases": [r.to_dict() for r in results],
    }


def _prices(runner: EvalRunner) -> dict[str, Any]:
    names = (
        [runner.model]
        if runner.model
        else sorted(
            {
                n
                for r in ("plan", "act", "summarize")
                for n in getattr(runner.settings.routing, r)
            }
        )
    )
    return {
        name: {
            "input_price_per_mtok": runner.settings.models[name].input_price_per_mtok,
            "output_price_per_mtok": runner.settings.models[name].output_price_per_mtok,
        }
        for name in names
    }


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _num(value: float | None, digits: int = 6) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _row(cells: list[str], widths: list[int]) -> str:
    """第一列左对齐，其余右对齐；按显示宽度补齐，中文占两格。"""

    out = []
    for n, (cell, width) in enumerate(zip(cells, widths, strict=True)):
        pad = " " * max(0, width - _width(cell))
        out.append(cell + pad if n == 0 else pad + cell)
    return "  ".join(out)


COLUMNS = ["策略", "成功率", "故障恢复率", "每成功成本(USD)", "每成功tokens", "请求数"]


def render(data: dict[str, Any]) -> str:
    widths = [max(10, _width(c)) for c in COLUMNS]
    lines = [
        f"评测模式：{'离线脚本' if data['mode'] == 'offline' else '在线模型 ' + data['model']}"
        f"，故障注入：{'开' if data['faults'] else '关'}"
    ]

    def block(title: str, metrics: dict[str, dict[str, Any]]) -> None:
        lines.extend(["", title, _row(COLUMNS, widths)])
        for strategy, m in metrics.items():
            cells = [
                STRATEGY_NAMES.get(strategy, strategy),
                _pct(m["success_rate"]),
                _pct(m["recovery_rate"]),
                _num(m["cost_per_success"]),
                _num(m["tokens_per_success"], 0),
                str(m["requests"]),
            ]
            lines.append(_row(cells, widths))

    for name, fam in data["families"].items():
        cases = next(iter(fam["metrics"].values()))["cases"]
        block(f"[{name}] {fam['title']}（{cases} 个用例）", fam["metrics"])
    block("[总计]", data["overall"])
    for name, reason in data["skipped"].items():
        lines.append(f"\n跳过任务族 {name}：{reason}")
    return "\n".join(lines)


def main(settings: Settings, args: argparse.Namespace) -> int:
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    families = load_families(args.families)
    if args.only:
        wanted = {f.strip() for f in args.only.split(",") if f.strip()}
        families = [f for f in families if f["family"] in wanted]
    runner = EvalRunner(
        settings,
        offline=args.offline,
        inject_faults=not args.no_faults,
        model=args.model,
    )
    data = runner.run(families, strategies, repeat=max(1, args.repeat))
    print(render(data))
    if args.out:
        Path(args.out).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n完整报告已写入 {args.out}")
    return 0


__all__ = ["STRATEGIES", "EvalRunner", "load_families", "main", "render", "report"]
