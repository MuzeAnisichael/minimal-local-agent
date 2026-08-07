# Security Policy

## Supported versions

Security fixes are applied to the latest release on the `main` branch.

## Reporting a vulnerability

Please do not open a public issue for an unpatched vulnerability. Use GitHub's
private vulnerability reporting feature for this repository. Include the affected
version, reproduction steps, impact, and any suggested mitigation.

## Security model

Minimal Local Agent is local-first, but model output is still untrusted input.
The runtime therefore:

- confines file tools to one configured workspace;
- rejects absolute paths and traversal outside that workspace;
- exposes no delete or shell-execution tool;
- requires interactive, per-call approval before every write;
- denies writes automatically in non-interactive environments;
- caps model turns, tool calls, output tokens, file sizes, and search results;
- records runs and tool metadata in a local SQLite audit log.

Do not point the workspace at a home directory, repository collection, or other
broad location containing secrets. Review model-requested writes before approving
them. A remote Ollama URL changes the privacy boundary because prompts and tool
results then leave the local machine.
