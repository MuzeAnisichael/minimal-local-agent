"""产物校验器：任务完成与否由确定性的检查判定，不听模型自己说“做完了”。

检查由任务提交者（宿主）提供，例如：
    {"type": "file_equals", "path": "a.txt", "content": "..."}
    {"type": "file_contains", "path": "a.txt", "text": "..."}
    {"type": "file_absent", "path": "tmp.txt"}
    {"type": "file_sha256", "path": "a.txt", "sha256": "..."}
    {"type": "answer_contains", "text": "..."}
    {"type": "command", "argv": ["python", "-m", "pytest", "-q"], "timeout_s": 120}
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from typing import Any

from minimal_local_agent.tools.fs import Workspace, sha256_text


@dataclass(frozen=True, slots=True)
class CheckResult:
    check: dict[str, Any]
    passed: bool
    detail: str
    artifact_path: str | None = None
    artifact_sha: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _file(ws: Workspace, path: str) -> tuple[str | None, str | None]:
    try:
        text = ws.read_optional(path)
    except (OSError, ValueError) as exc:
        return None, str(exc)
    return text, None


def run_check(check: dict[str, Any], ws: Workspace, answer: str) -> CheckResult:
    kind = check.get("type")
    path = check.get("path")
    if kind in {"file_equals", "file_contains", "file_sha256"}:
        text, error = _file(ws, path)
        if text is None:
            return CheckResult(check, False, error or f"missing: {path}", path)
        sha = sha256_text(text)
        if kind == "file_equals":
            ok = text == check["content"]
        elif kind == "file_contains":
            ok = check["text"] in text
        else:
            ok = sha == check["sha256"]
        return CheckResult(
            check, ok, "ok" if ok else f"content mismatch: {path}", path, sha
        )
    if kind == "file_absent":
        text, _ = _file(ws, path)
        ok = text is None and not ws.guard.resolve(path).exists()
        return CheckResult(check, ok, "ok" if ok else f"unexpected file: {path}", path)
    if kind == "answer_contains":
        ok = str(check["text"]).casefold() in answer.casefold()
        return CheckResult(
            check, ok, "ok" if ok else "answer does not contain expected text"
        )
    if kind == "command":
        try:
            done = subprocess.run(
                list(check["argv"]),
                cwd=ws.root,
                capture_output=True,
                text=True,
                timeout=float(check.get("timeout_s", 120)),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CheckResult(check, False, f"{type(exc).__name__}: {exc}")
        ok = done.returncode == 0
        tail = (done.stdout + done.stderr)[-1500:]
        return CheckResult(check, ok, "ok" if ok else f"exit {done.returncode}: {tail}")
    return CheckResult(check, False, f"unknown check type: {kind}")


def run_checks(
    checks: list[dict[str, Any]], ws: Workspace, answer: str
) -> list[CheckResult]:
    return [run_check(check, ws, answer) for check in checks]


def failure_report(results: list[CheckResult]) -> str:
    lines = [
        f"- {r.check.get('type')} {r.check.get('path', '')}: {r.detail}"
        for r in results
        if not r.passed
    ]
    return "\n".join(lines)


__all__ = ["CheckResult", "failure_report", "run_check", "run_checks"]
