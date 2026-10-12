# Changelog

All notable changes to this project are documented here.

## [1.1.0] - Unreleased

### Added

- Explicit optional `MemoryStore`, `Planner`, and `Peer` interfaces without
  enabling a multi-agent team by default.
- Scoped SQLite knowledge and structured entity records: candidate proposals,
  human approval/rejection, revision-checked updates, forgetting, and bounded
  approved-only retrieval. Entity attributes are not a graph or vector database.
- Model-free `memory list/show/propose/update/approve/reject/forget` commands,
  optional `[memory]` / `[planner]` settings, and `--memory` / `--planner` on
  runtime entry points.
- A single advisory `ModelPlanner` call charged to the shared run budget;
  structured steps and completion conditions do not execute a workflow.
- Explicit read-only `LocalPeer` delegation with separate workspace/database
  requirements, plus an optional A2A 1.0 HTTP+JSON adapter using the official
  Python SDK (`>=1.2.2,<1.3.0`) and a default-offline two-terminal example.
- A read-only `/inspector` with run, message, context, and memory views;
  SSE updates, globally correlated cursors, bounded event/content retention,
  stale-but-visible reconnect behavior, and separate persistent-memory snapshots.

### Changed

- Optional planning/delegation reserve shared request, tool, and output budgets.
  Missing usage stays unknown, and third-party A2A metadata is not a hard cost limit.
- Original conversation history and SDK model-submitted message views are kept
  distinct in observation. Message byte guards are not exact model token windows.
- Public documentation distinguishes the released v1.0 kernel from these local,
  unreleased extensions; existing v1.0 smoke results do not validate new paths.

### Fixed

- Undo file compensation and its SQLite audit/status now finish atomically.
- Local Web origin checks reject URLs containing credentials.
- The optional MCP extra constrains incompatible MCP 2.x, and integration tests
  no longer hide an installed but incompatible client behind an import skip.
- Unreported/null extension counters stay unknown; provisional peer usage never
  refunds a nonterminal task, and measured final model output is checked against
  the shared limit even if a provider ignores the requested maximum.
- SDK-owned model HTTP clients close on both success and failure and may be
  reopened for subsequent runs. Non-usage A2A artifacts preserve earlier counters.
- SSE streams actually close on exit, and mobile grids handle long local paths
  without widening the page.
- An in-place edit by a custom context reducer cannot rewrite archived messages.

### Boundaries

- Memory proposals require explicit human approval; the inspector has no memory
  editing, write-approval, cancellation, or administrative control endpoint.
- Peers are explicitly configured and loopback-only. A timeout stops waiting,
  not execution; cancellation and cross-process task recovery are unsupported.
- Raw observation is bounded in-memory data, not a durable full replay. Durable
  memory records and the existing history/audit databases have separate roles.
- The default single-agent loop, five filesystem tools, and private local
  connection configuration remain unchanged.

## [1.0.0] - 2026-09-30

### Added

- A documented 1.x compatibility contract for embedding/extensions, configuration,
  SQLite upgrades, execution receipts, and evaluation inputs/reports. The package
  root also exports `Settings`, `MCPServerSettings`, `AgentDependencies`,
  `ModelFactory`, and `EventHandler` alongside the existing public types.
- Frozen legacy SQLite fixtures and upgrade-preservation tests for schemas 0/1/2,
  plus failed-upgrade rollback and future-schema refusal tests.
- Fresh wheel/source install checks on Linux and Windows covering console entry
  points, embedding, audited tools, session history, receipts, and packaged Web.
- Reviewed task-family smoke results for local Ollama and a compatible API.

### Changed

- Version 1.0 is stable for the documented bounded kernel; Python 3.11+ and one
  active task per database/workspace remain the execution contract.
- TOML configuration rejects unknown fields and incorrect types. Correct v0.9
  settings remain valid; previously ignored misspellings must be corrected.
- Evaluation inputs reject unknown assertions and unsafe/non-POSIX fixture paths
  before model calls. Reports identify `minimal-local-agent.eval-report.v1`, the
  runtime version, and provider without adding connection configuration.
- The Web server returns HTTP 409 for overlapping tasks instead of risking lost
  session updates. Its run lock is released on both success and failure.
- Packaging uses SPDX MIT metadata and includes the release-check script.

### Fixed

- Pending SQLite migrations and version markers now commit atomically; a failed
  upgrade rolls back. A newer database is refused before enabling WAL.
- All-zero provider token metadata is reported as unknown, not zero-cost usage.
- The traversal dataset now checks the actual tool error instead of an unrelated
  word. Its regression test still requires an error event and forbids write tools.

The core retains five built-in tools, no shell/browser/sub-agent orchestration,
and no default context compression. The explicit reducer extension remains.

## [0.9.0] - 2026-09-29

### Added

- A configurable serialized-message byte budget checked before every model
  request, including requests after tool results. Overflow stops the run without
  silently dropping stored conversation history and is recorded in the receipt.
- An explicit host-owned context reducer seam for future compression. No automatic
  summarizer is included; reduced model views and their hashes are auditable while
  the full original history, including pre-reduction messages from the current run,
  stays in SQLite.
- Deterministic evaluation assertions for exact file contents and absent files.
- Task-family evaluation summaries with success rates and per-success latency and
  token usage. Unknown usage produces an unknown token metric, not an estimate.

### Changed

- The evaluation dataset v1 format accepts optional `family`, `expected_files`, and
  `absent_files` fields without breaking existing datasets.
- Evaluation fixtures are written as exact UTF-8 bytes, avoiding Windows newline
  conversion during artifact assertions.
- Every evaluation case now gets its own workspace and SQLite state database, so
  fixtures and conversation state cannot leak between cases.
- The roadmap now focuses v1.0 on API, configuration, and database compatibility,
  fresh-install packaging, and real-model regression evidence.

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
[0.9.0]: https://github.com/MuzeAnisichael/minimal-local-agent/compare/v0.8.0...v0.9.0
[1.0.0]: https://github.com/MuzeAnisichael/minimal-local-agent/compare/v0.9.0...v1.0.0
[1.1.0]: https://github.com/MuzeAnisichael/minimal-local-agent/compare/v1.0.0...main
