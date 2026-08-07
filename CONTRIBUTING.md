# Contributing

Thanks for helping keep Minimal Local Agent small and understandable.

## Development setup

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
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
6. Keep tool-event audit metadata useful without duplicating full file contents.
7. Add deterministic tests for every security boundary and bug fix.

Open an issue before proposing a large dependency, a shell tool, deletion, network
access, multi-agent orchestration, or a new persistence service.
