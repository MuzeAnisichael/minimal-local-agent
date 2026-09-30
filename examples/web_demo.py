"""Open the real Web console with disposable, public sample data only."""

from __future__ import annotations

import argparse
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from minimal_local_agent import Settings
from minimal_local_agent.web import serve_web


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    arguments = parser.parse_args()
    # Reuse the operator's ignored model configuration, never their workspace/state.
    settings = Settings.load()
    source = Path(__file__).with_name("demo_workspace")
    with tempfile.TemporaryDirectory(prefix="mla-demo-") as temporary:
        root = Path(temporary)
        workspace = root / "sample-workspace"
        shutil.copytree(source, workspace)
        settings = replace(
            settings,
            workspace=workspace,
            database=root / "demo.db",
            write_policy="deny",
            tool_policies=(),
            mcp_servers=(),
        )
        print("Disposable sample workspace; real model calls use your local config.")
        print("No personal files or saved sessions are loaded. Stop with Ctrl+C.")
        try:
            return serve_web(settings, port=arguments.port)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
