# Contributing

Thanks for helping keep Minimal Local Agent small and understandable.

## Development setup

```bash
python -m venv .venv
python -m pip install -e ".[dev,mcp]"
ruff format --check .
ruff check .
pytest
```

## Design rules

1. Keep one agent until a measured use case requires orchestration.
2. Prefer standard-library code and explicit interfaces over new dependencies.
3. Treat model output and tool arguments as untrusted input.
4. Keep tools narrow, bounded, testable, and auditable.
5. Remove capabilities when possible; require diff-backed confirmation otherwise.
6. Preserve transaction rollback, stale checks, undo safety, and receipt verification.
7. Keep tool-event audit metadata useful without duplicating full file contents.
8. Keep external tools optional, explicitly named, bounded, and disabled by default.
9. Add deterministic tests for every security boundary and bug fix.
10. Keep each independently verifiable behavior change in its own commit.

Register host-owned read tools through `ReadTool`, not a raw toolset. Test that
denied tools disappear from the model schema and that allowed calls enter audit
and receipts. Trusted Python code is not sandboxed; do not add dynamic imports of
tool modules from TOML.

Open an issue before proposing a large dependency, a shell tool, deletion, remote
network access, multi-agent orchestration, or a new persistence service.
