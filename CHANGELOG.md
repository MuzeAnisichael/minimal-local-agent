# Changelog

All notable changes to this project are documented here.

## [0.8.0] - 2026-09-28

### Added

- Explicit host-owned Python `ReadTool` declarations for `AgentRuntime` and
  `serve_web`, with policy-compiled model visibility, audit-safe call metadata,
  result bounds, and receipt inclusion.
- A runnable `examples/read_tool.py` showing an extension without modifying the
  agent core or loading arbitrary code from configuration.
- Tests for allowed, denied, oversized, and Web-hosted custom read tools.

### Changed

- `AgentRuntime(toolsets=..., external_tools=...)` is removed because raw toolsets
  bypassed the project's policy and audit boundary. Use `read_tools=(ReadTool(...),)`
  for trusted read-only Python extensions.
- The capability manifest identifies Python-hosted tools separately from MCP tools.
- Documentation distinguishes declared read-only host code from a sandbox.

## [0.6.0] - 2026-09-28

### Added

- A dependency-free, loopback-only local Web console with read-only and
  preview-only tasks, session history, run events, and receipt status.
- An explicit `openai-compatible` model provider for local servers, official
  APIs, and relay services implementing Chat Completions. API keys are read from
  a named environment variable and are never stored in TOML settings.
- Provider-neutral model connectivity checks shared by the CLI and Web console.
- Web and provider regression tests, including failure-session recovery.

### Changed

- The Web console displays effective capabilities and keeps session history
  accessible on small screens.
- Session lists include the latest prompt for easier navigation.
- Public example configuration remains Ollama-based; actual service addresses,
  credentials, and personal default models belong in ignored local settings.

### Security

- Web runs cannot approve file writes and the server only binds to loopback.
- API key URLs and insecure remote HTTP connections with keys are rejected.
- `.env` files are ignored by Git.

## [0.5.0] - 2026-08-10

### Added

- Transactional `edit_files` for exact, unique replacements across multiple files,
  with optional source hashes, one combined approval, stale checks, and rollback.
- Reversible change sets with `changes`, `change`, and guarded `undo` commands.
- Preview-only `--dry-run` mode that validates and renders mutations without writing.
- Stable `AgentRuntime` embedding API, replaceable model factory, capability policy
  compiler, and structured runtime events with optional JSONL CLI streaming.
- Versioned SQLite migrations and per-session hash-chained execution receipts with
  `receipts`, internal `verify`, and externally anchored `verify --head` commands.
- Per-tool policy overrides and a `capabilities` command showing the effective
  model-visible policy and fingerprint.
- Optional MCP client extra with loopback-only HTTP endpoints, explicit per-server
  tool allowlists, name prefixes, result limits, and audit-safe argument hashes.
- Versioned external JSON evaluation datasets, forbidden-tool assertions, expected
  error statuses, and an included adversarial boundary dataset.
- Optional MCP integration CI using a real local streamable-HTTP test server.

### Changed

- `write_file` now uses the same transaction engine as patch-oriented edits.
- Successful and failed model runs both receive execution receipts.
- Real-model evaluation reports the dataset name and clears unrelated tool/MCP
  policies for reproducibility.
- Documentation now describes v0.5 architecture, MCP trust boundaries, undo data,
  embedding APIs, and remaining non-goals.

### Security

- Write tools cannot be configured to bypass approval.
- Denied read, write, and MCP capabilities are omitted from the actual tool surface.
- MCP server instructions, sampling, elicitation, roots, implicit tool exposure,
  remote endpoints, and oversized results are rejected or disabled.
- Undo refuses to overwrite files changed after the original transaction.

## [0.2.0] - 2026-08-08

### Added

- `confirm` and `deny` write policies; `deny` removes `write_file` from the model's
  registered tools.
- Bounded unified-diff previews, including visible line-ending changes, before
  write approval.
- `minimal-agent audit` with human-readable and JSON output.
- `minimal-agent eval` for isolated list/read/search checks across Ollama models.
- English and Chinese comparison documents covering similar local-agent systems.

### Changed

- Model-facing list and search schemas no longer expose glob parameters; both
  recurse by default, reducing invalid argument choices for small local models.
- Approved writes are revalidated before atomic replacement.
- Write audit events include byte counts and a diff hash without storing full file
  contents in the event row.
- The bilingual README now documents the explicit trust boundary and limitations.

## [0.1.0] - 2026-08-08

### Added

- Initial single-agent loop with Ollama and PydanticAI.
- Workspace-confined list, read, search, and confirmed write tools.
- SQLite sessions, run history, and tool events.
- TOML configuration, CLI, tests, CI, and security documentation.

[0.2.0]: https://github.com/MuzeAnisichael/minimal-local-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/MuzeAnisichael/minimal-local-agent/releases/tag/v0.1.0
[0.5.0]: https://github.com/MuzeAnisichael/minimal-local-agent/compare/v0.2.0...v0.5.0
[0.6.0]: https://github.com/MuzeAnisichael/minimal-local-agent/compare/v0.5.0...v0.6.0
[0.8.0]: https://github.com/MuzeAnisichael/minimal-local-agent/compare/v0.6.0...v0.8.0
