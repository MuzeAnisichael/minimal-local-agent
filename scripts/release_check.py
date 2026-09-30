"""Check distribution contents and exercise fresh CLI, embedding, and Web installs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from importlib.metadata import version
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen


def smoke(installed_root: Path | None = None) -> None:
    from pydantic_ai.models.test import TestModel

    import minimal_local_agent as package
    from minimal_local_agent import AgentRuntime, ReadTool, Settings
    from minimal_local_agent.web import create_web_server

    assert package.__version__ == version("minimal-local-agent")
    if installed_root is not None:
        assert Path(package.__file__).resolve().is_relative_to(installed_root.resolve())

    class Factory:
        def __init__(self, tools: tuple[str, ...] = ()) -> None:
            self.tools = tools

        def create(self, _settings: Settings) -> TestModel:
            return TestModel(call_tools=list(self.tools), custom_output_text="done")

    def marker() -> str:
        """Return a deterministic release marker."""
        return "release-check"

    with tempfile.TemporaryDirectory(prefix="mla-smoke-") as temporary:
        root = Path(temporary)
        settings = Settings(
            workspace=root / "workspace",
            database=root / "state.db",
            write_policy="deny",
        )
        runtime = AgentRuntime(
            settings,
            model_factory=Factory(("release_marker",)),
            read_tools=(ReadTool("release_marker", marker),),
        )
        first = runtime.run("first")
        second = runtime.run("second", session_id=first.session_id)
        assert second.response == "done"
        assert len(runtime.store.get_runs(first.session_id)) == 2
        assert runtime.store.get_tool_events(first.session_id)[0]["status"] == "ok"
        assert "first" in runtime.store.load_history(first.session_id)
        assert runtime.store.verify_receipt_chain(
            first.session_id, expected_head=second.receipt_hash
        ) == (True, None)

        console = Path(sys.executable).parent / (
            "minimal-agent.exe" if os.name == "nt" else "minimal-agent"
        )
        environment = {k: v for k, v in os.environ.items() if not k.startswith("MLA_")}
        environment.update(
            MLA_WORKSPACE=str(settings.workspace),
            MLA_DATABASE=str(settings.database),
            MLA_WRITE_POLICY="deny",
        )
        result = subprocess.run(
            [str(console), "--version"],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.stdout.strip() == package.__version__
        result = subprocess.run(
            [str(console), "capabilities", "--json"],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert len(json.loads(result.stdout)["policy_sha256"]) == 64

        server = create_web_server(
            settings,
            port=0,
            runtime_factory=lambda configured: AgentRuntime(
                configured, model_factory=Factory()
            ),
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            for path in ("/", "/assets/app.js", "/assets/app.css"):
                with urlopen(address + path, timeout=5) as response:
                    assert response.status == 200 and response.read()
            request = Request(
                address + "/api/run",
                data=b'{"prompt":"web smoke","mode":"preview"}',
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=10) as response:
                outcome = json.load(response)
            assert outcome["response"] == "done" and outcome["mode"] == "preview"
            with urlopen(
                address + "/api/sessions/" + outcome["session_id"], timeout=5
            ) as response:
                assert json.load(response)["receipt_chain"]["verified"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print(f"{package.__version__}: CLI, embedding, audited tool, and Web smoke passed")


def check_archive(distribution: Path) -> None:
    if distribution.suffix == ".whl":
        with zipfile.ZipFile(distribution) as archive:
            names = archive.namelist()
    else:
        with tarfile.open(distribution) as archive:
            names = archive.getnames()
    for name in names:
        parts = Path(name).parts
        assert not any(p == "agent.toml" or p.startswith(".env") for p in parts)
        assert not name.endswith((".db", "-wal", "-shm"))
    for required in (
        "context.py",
        "evals.py",
        "web_assets/index.html",
        "web_assets/app.js",
    ):
        qualified = "minimal_local_agent/" + required
        assert any(
            name == qualified or name.endswith("/" + qualified) for name in names
        )
    if distribution.suffix != ".whl":
        assert any(name.endswith("/docs/COMPATIBILITY.md") for name in names)
        assert any(name.endswith("/tests/fixtures/legacy_store.sql") for name in names)


def fresh_installs(directory: Path) -> None:
    wheels = sorted(directory.glob("*.whl"))
    sources = sorted(directory.glob("*.tar.gz"))
    assert len(wheels) == len(sources) == 1, (
        "Build exactly one wheel and one source distribution"
    )
    distributions = wheels + sources
    for distribution in distributions:
        check_archive(distribution)
        with tempfile.TemporaryDirectory(prefix="mla-install-") as temporary:
            root = Path(temporary)
            environment_path = root / "venv"
            venv.EnvBuilder(with_pip=True).create(environment_path)
            python = environment_path / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "--disable-pip-version-check",
                    "install",
                    "--timeout",
                    "30",
                    "--retries",
                    "1",
                    str(distribution.resolve()),
                ],
                cwd=root,
                env=environment,
                check=True,
                timeout=300,
            )
            subprocess.run(
                [
                    str(python),
                    str(Path(__file__).resolve()),
                    "--installed-root",
                    str(environment_path),
                ],
                cwd=root,
                env=environment,
                check=True,
                timeout=120,
            )
        print(f"Fresh installation passed: {distribution.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distributions", type=Path)
    parser.add_argument("--installed-root", type=Path)
    arguments = parser.parse_args()
    if arguments.distributions:
        fresh_installs(arguments.distributions)
    else:
        smoke(arguments.installed_root)
