# Comparison: small and local agent systems

Research date: 2026-08-10. This comparison uses official project documentation
and repositories. “Trade-off” describes scope and responsibility, not a defect.

## Short conclusion

Minimal Local Agent is not trying to win on feature count. Its niche is a **small,
verifiable local-agent kernel**:

1. policy removes capabilities from the real tool schema;
2. multi-file changes are previewed, approved, committed, and undone as bounded
   transactions;
3. history, tool metadata, runtime events, and hash-chained receipts are inspectable;
4. local models are evaluated against the exact deployed tool surface;
5. shell, deletion, remote networking, daemons, and hidden orchestration stay out
   of the default boundary.

## At a glance

| Project | Optimizes for | Main strengths | Trade-off relative to this project |
|---|---|---|---|
| Minimal Local Agent | Verifiable local filesystem work | Small policy-compiled surface, transactional edits/undo, receipts, local eval, guarded MCP | No shell, UI, channels, browser, semantic memory, remote MCP, or multi-agent support |
| [smolagents](https://huggingface.co/docs/smolagents/main/index) | Minimal programmable agent library | Concise API, model/tool independence, capable code agents, local models, MCP | Persistence, workspace policy, and durable audit remain application responsibilities; code execution needs stronger isolation for hostile workloads |
| [Qwen-Agent](https://qwenlm.github.io/Qwen-Agent/en/guide/get_started/features/) | Feature-rich agents around Qwen | Qwen tool parsing, parallel/multi-step calls, RAG, built-in tools, MCP, streaming, Gradio | Broader and more model-centric; least-privilege filesystem policy and receipts are application concerns |
| [nanobot](https://github.com/HKUDS/nanobot) | Lightweight self-hosted personal assistant | Channels, web UI, memory, scheduling, MCP, shell/web/file tools, sub-agents | Much larger credential, process, network, and operational boundary |
| [Goose](https://github.com/aaif-goose/goose) | Polished extensible desktop/CLI agent | Multiple providers, desktop/CLI/API, MCP ecosystem, rich permission modes | Larger runtime and extension surface with more policy choices to reason about |

## Detailed trade-offs

### smolagents

smolagents makes model-plus-tool experiments unusually concise and supports both
tool-calling and code-agent styles. It is the better choice when programmability,
provider breadth, or generated code execution is the main requirement.

Minimal Local Agent supplies a ready workspace boundary, confirmation flow, SQLite
state, reversible mutations, and receipts. It deliberately avoids model-generated
code. The smolagents
[secure execution guide](https://huggingface.co/docs/smolagents/main/tutorials/secure_code_execution)
also distinguishes restricted local execution from stronger Docker or remote
sandbox isolation.

### Qwen-Agent

Qwen-Agent is stronger when an application wants deep Qwen-specific behavior,
parallel calls, RAG, code interpretation, web/image tools, streaming, or a Gradio
path. It also publishes a
[planning benchmark](https://qwenlm.github.io/Qwen-Agent/en/benchmarks/).

Minimal Local Agent remains model-independent at the application boundary through
Ollama's compatible endpoint. It tests concrete file-tool behavior and ships a
fixed least-privilege filesystem policy rather than a broad tool platform.

### nanobot

nanobot is already a personal-assistant system: channels, memory, scheduling,
web/file/shell tools, MCP, sub-agents, and automation are central features. That is
more useful for an always-on assistant, but it necessarily creates more secrets,
services, and powerful capabilities to protect.

Minimal Local Agent has no long-running service or channel credentials. It keeps
memory transparent and local, and excludes process execution and remote network
tools from the core.

### Goose

Goose provides native desktop, CLI, and API experiences, many model providers, a
large MCP ecosystem, and multiple approval modes. See its official
[permission modes](https://goose-docs.ai/docs/guides/goose-permissions/) and
[tool permissions](https://goose-docs.ai/docs/guides/managing-tools/tool-permissions/).

Minimal Local Agent physically omits denied schemas, limits confirmation to bounded
workspace mutations, and keeps five built-in tools inspectable at once. Its MCP path
is intentionally narrower: loopback HTTP, explicit tool names, no server-supplied
instructions or model sampling, and bounded results.

## Where Minimal Local Agent is stronger

- **Understandable boundary:** five built-in tools and one policy compiler.
- **Least privilege:** denied tools never reach the model schema.
- **Reviewable mutation:** one approval covers a complete bounded diff.
- **Recoverability:** transactions roll back on failure and can later be undone if
  current hashes still match.
- **Forensic value:** local history, tool metadata, events, policy fingerprints, and
  per-session receipt chains.
- **Measured compatibility:** expected answers must be backed by expected tool
  events; external datasets can assert statuses and forbidden tools.
- **Low operations:** no queue, vector database, browser, daemon, or process sandbox
  is required because process execution is absent.

## Where it is weaker

- scope is bounded text-file work, not general desktop or personal assistance;
- exact replacement is narrower than arbitrary patch syntax;
- SQLite history is not semantic long-term memory;
- no token streaming or graphical interface;
- current datasets are regression smoke tests, not statistical benchmarks;
- MCP is loopback HTTP only and trusts the operator's read-only allowlist;
- no process/container sandbox because no process tool is exposed.

## Completed priorities and next work

| State | Improvement | Value | Constraint |
|---|---|---|---|
| v0.2 | Mechanical read-only mode and diff approval | Enforceable capability boundary | Denied tools stay absent from schema |
| v0.3 | Transactional exact edits, dry run, rollback, undo | Safer editing without arbitrary patch parsing | Bound file count and complete diff |
| v0.4 | Runtime API, policy engine, events, migrations, receipts | Explicit embedding and verification | Strong tamper evidence requires saving the chain head externally |
| v0.5 | Loopback allowlisted MCP and external eval datasets | Measured extension points | No implicit tools or remote MCP |
| Next | Signed receipt export | Portable verification | Key custody remains outside the model boundary |
| Next | Provenance-preserving context compaction | Longer useful sessions | No opaque semantic memory |
| Later | Async streaming with complete persistence | Lower perceived latency | Final history and receipts remain complete |

## Admission rule for new capabilities

“Minimal” is a continuing constraint. Every proposed capability should answer:

1. Which measured task cannot be solved by the current surface?
2. What is the smallest permission that solves it?
3. How will it be bounded, tested, audited, and reversed?

If those answers are unclear, the feature belongs in an adapter or a broader agent
platform, not in the core.
