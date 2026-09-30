# Contributing

Thanks for helping keep Minimal Local Agent small and understandable.

English and Chinese reports and pull requests are welcome.
欢迎用中文或英文参与；优先提交小型、可验证的修复、示例和文档改进。

## Choose the right entry point

- Read the [documentation](docs/README.md) or [中文指南](docs/README.zh-CN.md)
  for usage and configuration.
- Use the [issue templates](https://github.com/MuzeAnisichael/minimal-local-agent/issues/new/choose)
  for reproducible bugs or small improvements.
- Follow [SECURITY.md](SECURITY.md) privately for vulnerabilities, not a public issue.
- For a new capability, first read the [roadmap](docs/ROADMAP.md) and
  [admission rule](docs/COMPARISON.md#admission-rule-for-new-capabilities).
  A host-owned adapter or example is usually a better starting point than a core module.

Use synthetic fixture files in reports. Never publish keys, actual connection
URLs, local configuration, personal paths, database exports, or unreviewed logs.

## Development setup

Clone the repository and activate a Python 3.11+ virtual environment as shown in
the [quick start](README.md#quick-start), then install development extras:

```bash
python -m pip install -e ".[dev,mcp]"
ruff format --check .
ruff check .
pytest
```

Unit tests require no live model. `minimal-agent doctor` checks a configured
endpoint; `minimal-agent eval` makes real model calls and may incur usage charges.
For UI work, `python examples/web_demo.py` isolates sample files and state from
your personal workspace.

## Before opening a pull request

Explain the problem, scope, and verification. Keep independently verifiable slices
in separate commits. Add deterministic tests for changed behavior, update both
language guides when user-facing instructions change, and check local links.
Do not bump the version for presentation-only edits or claim unmeasured performance.

Read [COMPATIBILITY.md](docs/COMPATIBILITY.md) before changing public signatures,
configuration fields, SQLite migrations, or evaluation data. A 1.x maintenance
change must preserve those contracts and include a regression test.

## Release checks

```bash
python -m pip install build
python -m build --outdir dist
python scripts/release_check.py --distributions dist
```

Use a clean output directory containing exactly one wheel and one source archive.
The check installs both in separate temporary environments, verifies packaged
assets and private-file exclusions, and exercises CLI, embedding, audit/history,
receipts, and Web without a live model. CI also checks the oldest supported
PydanticAI version and the optional MCP extra. Run real-model evaluations separately
and publish only reviewed, connection-free summaries.

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
