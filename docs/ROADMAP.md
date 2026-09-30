# Roadmap

Minimal Local Agent remains a local-first, single-agent kernel for bounded
workspace work. CLI and loopback Web are the two interfaces; neither requires a
planner, background worker, or broad plugin platform.

## v0.9 alpha — foundation completed

- Guard serialized model-message bytes before every request. Reject overflow by
  default, keep the full saved history, and expose an explicit host-owned reducer
  seam for a future auditable compression strategy.
- Let isolated evaluation cases assert exact file contents and absent files.
  These are evaluation checks, not a general post-write rollback promise.
- Group evaluation cases by task family and report success rate, per-success
  latency, and per-success tokens under recorded fixed call limits. Unknown token
  usage stays unknown; no provider price is inferred.

## v1.0 stable — completed

1. Published the [v1 compatibility contract](COMPATIBILITY.md): public host API,
   strict local configuration, dataset/report formats, and atomic upgrades from
   supported SQLite schemas. Newer database schemas are refused without modification.
2. Added fresh source/wheel installation checks for Linux and Windows, including
   CLI, embedding, audit/history, receipts, and Web. CI covers minimum/current
   compatible dependencies and the optional MCP integration.
3. Recorded small real-model regressions on Ollama and a compatible API, with
   isolated answer/tool/file assertions and reviewed public results. Clarified the
   single-active-task boundary and rejected overlapping Web runs.

The planned minimal product is complete. v1.x is maintenance: compatibility fixes,
regression tests, and documentation. Customization should first use the existing
model factory, audited read tools, explicit MCP allowlists, and context reducer seam.

Automatic context compression remains optional host-owned work, not a default
feature. Add compression, cancellation, or broader recovery only for a measured
failure mode and with tests. Planner hierarchies, hidden sub-agents, shell/browser
tools, and general checkpoint orchestration remain outside the default kernel.
