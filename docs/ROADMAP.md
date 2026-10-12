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

The original minimal product remains the baseline. Customization should first use
the model factory, audited read tools, explicit MCP allowlists, and context reducer.

## v1.1 modular extensions — local, unreleased

- Add optional scoped SQLite knowledge/entity memory: candidates, explicit human
  approval, revision checks, and bounded retrieval. No vector service or graph engine.
- Add one advisory planning call within the shared run budget. It is not an
  automatic workflow, task scheduler, or completion certificate.
- Add explicit read-only `Peer` interfaces and an optional official-SDK A2A 1.0
  HTTP+JSON adapter. Keep separate local workspaces/databases, loopback endpoints,
  timeout-as-wait semantics, and no cross-process restart recovery or cancellation.
- Add a read-only live inspector separating original messages, model-submitted
  message views, byte budgets, persistent memory, and task state. Retain stale
  content on disconnect and show bounded-window gaps explicitly.

The default stays a single agent with these optional modules disabled. Details
and configuration are in the [module guide (中文)](MODULES.md). v1.1.0 is not yet a
published release; the v1.0 validation record does not establish acceptance for it.

## Local acceptance completed; publication remains separate

Default-off behavior, approval/revision conflicts, shared budgets, protocol
boundaries, inspector live/stale/gap behavior, and mutable-history isolation have
local regression evidence. Fresh Windows packaging and Linux minimum/current
source checks also passed; the implemented CLI and documentation were reconciled.
See [v1.1 verification](VALIDATION.md). Live-model quality and macOS remain unverified.
Publishing a tag, GitHub Release, and running public CI require a separate release
action; none is implied by local verification.

## v1.x maintenance

Keep ports explicit and implementations replaceable. Prefer compatibility fixes,
measured reliability issues, small regressions, and documentation over new layers.

Automatic context compression remains optional host-owned work, not a default
feature. Add compression, cancellation, or broader recovery only for a measured
failure mode and with tests. Planner hierarchies, hidden teams, vector/graph
services, shell/browser tools, and general checkpoint orchestration remain outside
the default kernel.
