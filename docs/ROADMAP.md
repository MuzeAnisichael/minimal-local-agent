# Roadmap

Minimal Local Agent remains a local-first, single-agent kernel for bounded
workspace work. CLI and loopback Web are the two interfaces; neither requires a
planner, background worker, or broad plugin platform.

## v0.9 alpha — current

- Guard serialized model-message bytes before every request. Reject overflow by
  default, keep the full saved history, and expose an explicit host-owned reducer
  seam for a future auditable compression strategy.
- Let isolated evaluation cases assert exact file contents and absent files.
  These are evaluation checks, not a general post-write rollback promise.
- Group evaluation cases by task family and report success rate, per-success
  latency, and per-success tokens under recorded fixed call limits. Unknown token
  usage stays unknown; no provider price is inferred.

## v1.0 stable — release gates

1. Document and stabilize the Python extension API, local configuration, and
   evaluation dataset format; test SQLite upgrades from supported older versions.
2. Verify a clean source/wheel installation on supported platforms and run a
   small real-model regression on local Ollama plus one compatible endpoint.
3. Publish reproducible task-family results and recheck CLI/Web workflows,
   permission boundaries, privacy guidance, and migration notes before the tag.

Add automatic context compression, task cancellation, or a broader recovery
mechanism only for a measured failure mode. Planner hierarchies, hidden
sub-agents, shell/browser tools, and general checkpoint orchestration remain
outside the default kernel.
