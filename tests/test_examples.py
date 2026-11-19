from __future__ import annotations

import importlib.util
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_offline_demo_runs(capsys) -> None:
    spec = importlib.util.spec_from_file_location(
        "offline_demo", EXAMPLES / "offline_demo.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main() == 0
    out = capsys.readouterr().out
    assert "tool.reconciled" in out
    assert "artifact.written  config.ini" in out
