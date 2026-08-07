# Changelog

All notable changes to this project are documented here.

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
