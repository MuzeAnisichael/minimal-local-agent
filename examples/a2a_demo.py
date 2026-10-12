"""Two-terminal A2A demo. Default server uses TestModel; no model API calls.

Install ``pip install -e ".[a2a]"`` first. Terminal one:
    python examples/a2a_demo.py serve
Terminal two:
    python examples/a2a_demo.py call "Inspect the demo workspace"

To use a real model, explicitly provide --config with a separate peer workspace
and database. Connection details remain in that local configuration.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from minimal_local_agent.a2a_adapter import A2APeer, build_a2a_app
from minimal_local_agent.config import Settings
from minimal_local_agent.peers import LocalPeer, loopback_endpoint


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "call"))
    parser.add_argument(
        "task", nargs="?", default="Describe your read-only capabilities"
    )
    parser.add_argument("--endpoint", default="http://127.0.0.1:9051")
    parser.add_argument(
        "--config", type=Path, help="Opt in to a live, separate peer config"
    )
    args = parser.parse_args()
    endpoint = loopback_endpoint(args.endpoint)
    if args.mode == "call":
        result = A2APeer(endpoint).call(
            args.task,
            timeout=30,
            request_limit=2,
            tool_calls_limit=2,
            max_output_tokens=1024,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit('Install A2A support: pip install -e ".[a2a]"') from exc
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http":
        parser.error("The demo supports HTTP only; configure HTTPS in your ASGI host")
    if args.config:
        peer = LocalPeer(Settings.load(args.config))
    else:
        from pydantic_ai.models.test import TestModel

        class DemoModelFactory:
            def create(self, _settings: Settings) -> TestModel:
                return TestModel(
                    call_tools=[],
                    custom_output_text="Offline A2A demo done; no model API called.",
                )

        peer = LocalPeer(
            Settings(
                workspace=Path(".minimal-local-agent/a2a-demo/workspace").resolve(),
                database=Path(".minimal-local-agent/a2a-demo/state.db").resolve(),
            ),
            model_factory=DemoModelFactory(),
        )
    uvicorn.run(build_a2a_app(peer, endpoint), host="127.0.0.1", port=parsed.port or 80)


if __name__ == "__main__":
    main()
