"""M4：评测集与评测运行器。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minimal_local_agent.cli import main as cli_main
from minimal_local_agent.config import Settings
from minimal_local_agent.eval import faults
from minimal_local_agent.eval.runner import (
    STRATEGIES,
    EvalRunner,
    load_families,
    render,
)

FAMILIES = Path(__file__).resolve().parents[1] / "evals" / "families"
CHECK_TYPES = {
    "file_equals",
    "file_contains",
    "file_absent",
    "file_sha256",
    "answer_contains",
    "command",
}


def _families(*names: str) -> list[dict]:
    return [f for f in load_families(FAMILIES) if f["family"] in names]


def test_dataset_is_well_formed() -> None:
    families = load_families(FAMILIES)
    assert len(families) >= 5
    for family in families:
        ids = [c["id"] for c in family["cases"]]
        assert len(ids) == len(set(ids)), family["family"]
        for case in family["cases"]:
            assert case["goal"].strip() and case["checks"] and case["script"]["turns"]
            assert {c["type"] for c in case["checks"]} <= CHECK_TYPES
            fault = case.get("fault")
            if fault:
                assert fault["type"] in {*faults.FAULTS, "bad_call"}


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> dict:
    families = _families(
        "read_qa", "single_edit", "multi_file", "subtask", "memory_reuse"
    )
    runner = EvalRunner(
        Settings(), offline=True, workdir=tmp_path_factory.mktemp("eval")
    )
    return runner.run(families, list(STRATEGIES))


def test_recovery_strategies_recover_every_injected_fault(report) -> None:
    for strategy in ("recovery", "full"):
        m = report["overall"][strategy]
        assert m["fault_cases"] > 0 and m["recovery_rate"] == 1.0, m
    assert report["overall"]["baseline"]["recovery_rate"] < 1.0
    assert report["overall"]["full"]["success_rate"] == 1.0


def test_memory_lowers_cost_per_success_on_related_tasks(report) -> None:
    fam = report["families"]["memory_reuse"]["metrics"]
    assert fam["memory"]["success_rate"] > fam["baseline"]["success_rate"]
    assert fam["memory"]["cost_per_success"] < fam["baseline"]["cost_per_success"]


def test_crashed_cases_are_resumed_only_with_recovery(report) -> None:
    crashed = [c for c in report["cases"] if c["crashed"]]
    assert crashed
    for case in crashed:
        recovering = STRATEGIES[case["strategy"]]["recovery"]["enabled"]
        assert case["resumed"] == recovering
        assert case["ok"] == recovering, case


def test_report_renders_and_costs_are_counted(report) -> None:
    text = render(report)
    assert "成功率" in text and "[总计]" in text
    assert report["overall"]["baseline"]["cost_usd"] > 0
    assert all(c["usage"]["requests"] > 0 for c in report["cases"])


def test_cli_eval_writes_report(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "report.json"
    code = cli_main(
        [
            "eval",
            "--offline",
            "--families",
            str(FAMILIES),
            "--strategies",
            "baseline",
            "--no-faults",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    data = json.loads(out.read_text("utf-8"))
    assert data["mode"] == "offline" and not data["faults"]
    assert "基础循环" in capsys.readouterr().out
