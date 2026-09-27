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
- exposes no delete, shell-execution, browser, or arbitrary network tool;
- removes denied tools entirely from the model-visible schema;
- stages exact multi-file edits and shows one bounded unified diff before approval;
- supports preview-only runs and revalidates every file before replacement;
- rolls back partially applied transactions and rejects stale undo operations;
- denies writes automatically in non-interactive environments;
- caps model turns, tool calls, output tokens, files, searches, transactions, diffs,
  and MCP results;
- restricts optional MCP clients to loopback HTTP endpoints with explicit tool
  allowlists and no server instructions, sampling, elicitation, or roots;
- records runs, tool metadata, reversible snapshots, and hash-chained execution
  receipts in local SQLite.
- serves the optional Web console only on loopback, with Host and same-origin
  checks, and never permits it to approve writes.

Do not point the workspace at a home directory, repository collection, or other
broad location containing secrets. Review model-requested writes before approving
them; a diff preview is not a guarantee that the model's change is correct. A
remote model URL changes the privacy boundary because prompts and tool results
then leave the local machine. A compatible official API or relay is remote even
when accessed through a locally configured endpoint.

The optional `openai-compatible` adapter reads a secret from the environment
variable named in local `api_key_env`; never put a key in `agent.toml` or a URL.
Remote keyed connections must use HTTPS. Keep local `agent.toml` and `.env` files
out of Git. The Web console is intended only for the operator's own machine;
do not reverse-proxy it or expose it on a network interface.

## MCP boundary

The v0.5 MCP adapter is intended for trusted local servers exposing tools the
operator has verified as read-only. An allowlist limits exposure; it cannot prove a
server's implementation is side-effect-free. Tool descriptions and returned content
may contain prompt injection. Do not connect an untrusted server or allowlist a tool
that writes, executes, authenticates, purchases, or sends messages.

MCP audit rows store argument names and a canonical argument hash, not full
arguments or full results. This reduces duplication but does not prevent the remote
model service from seeing tool results when `base_url` is non-local.

## Local sensitive data

Message history can include complete text returned by read tools. Undo snapshots
contain the pre-change contents of edited files. Execution receipts contain hashes,
policy manifests, usage, and audit-safe tool metadata. Protect and back up the
SQLite database with the same care as the workspace.

Receipt verification detects broken links or content changed without updating its
hash. If the last hash is saved outside the database, `verify --head HASH` also
detects chain rewriting or truncation. Without that external anchor, anyone able to
write the database can recompute the entire chain. Receipts are not digitally signed,
do not identify the machine or user, and are not remote attestation.
