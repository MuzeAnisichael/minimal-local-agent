# Comparison: small and local agent systems

Research date: 2026-08-08. This comparison uses project documentation and source
repositories rather than secondary reviews.

## Short conclusion

Minimal Local Agent should not compete with broader projects on integrations. Its
useful niche is a **small, verifiable local-agent baseline**:

1. capabilities are removed mechanically, not only discouraged in prompts;
2. every write is validated and shown as a unified diff before approval;
3. tool behavior is stored in a queryable audit trail;
4. installed local models can be checked with the same isolated evaluation;
5. shell, deletion, networking, daemons, and hidden orchestration stay out of the
   default trust boundary.

## At a glance

| Project | Optimizes for | Main strengths | Trade-off relative to this project |
|---|---|---|---|
| Minimal Local Agent | Verifiable local filesystem work | Small tool surface, strict read-only mode, diff approval, SQLite audit, built-in local eval | No shell, UI, channels, MCP, browser, semantic memory, or multi-agent support |
| [smolagents](https://huggingface.co/docs/smolagents/main/index) | Minimal programmable agent library | Concise API, model/tool agnostic, capable code agents, local model support, MCP | A framework rather than a complete persistence/audit product; code execution needs a stronger sandbox for hostile workloads |
| [Qwen-Agent](https://qwenlm.github.io/Qwen-Agent/en/guide/get_started/features/) | Feature-rich agents around Qwen models | Strong Qwen tool parsing, parallel/multi-step calls, RAG, built-in tools, MCP, streaming and Gradio UI | Broader and more model-centric; least-privilege file policy and durable audit are application responsibilities |
| [nanobot](https://github.com/HKUDS/nanobot) | Lightweight self-hosted personal assistant | Chat channels, web UI, memory, scheduling, MCP, shell/web/file tools, sub-agents and automation | Much larger capability surface and operational boundary; production hardening needs more configuration |
| [Goose](https://github.com/aaif-goose/goose) | Polished extensible desktop/CLI agent | Multiple providers, desktop/CLI/API, many MCP extensions and permission modes | Larger runtime and tool surface; more policy and extension choices to reason about |

The projects solve overlapping but different problems. “Trade-off” does not mean
defect; it shows which responsibilities move to the adopter.

## Detailed analysis

### smolagents

What it does especially well:

- turns a model plus tools into an agent with very little code;
- supports hosted and local models, including Ollama and Transformers;
- offers both tool-calling and code-agent styles;
- integrates external tools and MCP without imposing a large application shell.

Where Minimal Local Agent differs:

- provides a ready persistence, audit, workspace, and confirmation policy;
- intentionally avoids model-generated code execution;
- treats the CLI behavior and SQLite schema as part of the reference architecture.

The smolagents [secure execution guide](https://huggingface.co/docs/smolagents/main/tutorials/secure_code_execution)
states that its local Python executor restricts imports and operations but cannot be
made completely secure; robust isolation uses Docker or remote sandboxes. That is a
reasonable design for code agents, while this project chooses not to expose code
execution at all.

### Qwen-Agent

What it does especially well:

- handles Qwen-specific function-calling formats and context management;
- supports parallel and multi-step tool use;
- includes RAG, code interpreter, web, image, MCP, streaming, and Gradio paths;
- publishes a [planning benchmark](https://qwenlm.github.io/Qwen-Agent/en/benchmarks/)
  for more complex tasks.

Where Minimal Local Agent differs:

- remains model-independent at the application boundary through Ollama's compatible
  API;
- keeps only three read capabilities plus an optional write capability;
- evaluates concrete local tool compatibility rather than broad planning quality;
- supplies a strict filesystem and audit policy out of the box.

### nanobot

What it does especially well:

- is already a personal-agent system rather than only a core loop;
- connects chat channels and a web UI;
- includes long-term memory, scheduling, web/file/shell tools, MCP and sub-agents;
- supports ongoing automation and API-driven use.

Where Minimal Local Agent differs:

- has no long-running service or channel credentials to protect;
- excludes shell and network tools instead of sandboxing or approving them;
- confines all file tools to the workspace by default;
- keeps memory to transparent SQLite session history.

nanobot's own deployment guidance recommends workspace restriction and, on Linux,
additional shell sandboxing for production. Those controls make sense for its much
broader assistant scope. Minimal Local Agent narrows the scope so those capabilities
are absent by construction.

### Goose

What it does especially well:

- provides native desktop, CLI, and API experiences;
- supports many model providers and a large MCP extension catalog;
- offers Autonomous, Manual Approval, Smart Approval, and Chat Only modes;
- supports per-tool Always Allow, Ask Before, and Never Allow controls.

Official references: [permission modes](https://goose-docs.ai/docs/guides/goose-permissions/)
and [tool permissions](https://goose-docs.ai/docs/guides/managing-tools/tool-permissions/).

Where Minimal Local Agent differs:

- `deny` physically omits the write schema rather than relying on classification;
- confirmation is limited to one well-understood side effect;
- the full built-in tool set remains small enough to inspect at once;
- evaluation targets locally installed Ollama models with that exact tool set.

A Goose [Qwen/Ollama issue](https://github.com/aaif-goose/goose/issues/6883)
reports tool-use degradation when many tools are exposed. It is one issue report,
not a general benchmark, but it supports measuring small local models against the
actual tool surface instead of assuming compatibility.

## Advantages and disadvantages of Minimal Local Agent

### Advantages

- **Understandable trust boundary:** four tools at most, all in one module.
- **Least privilege:** read-only mode removes mutation capability.
- **Reviewable changes:** approval includes the proposed textual diff.
- **Forensic value:** prompts, outcomes, usage, and tool metadata persist locally.
- **Comparable local behavior:** model evaluation requires both the right answer
  and a successful expected tool event.
- **Low operational burden:** no server, queue, vector database, browser, or daemon.

### Disadvantages

- useful only for bounded text-file tasks today;
- full-file writes are less efficient than patch-based editing;
- SQLite history is not semantic long-term memory;
- no streaming output or polished graphical interface;
- the three evaluation cases are a smoke test, not a statistically robust benchmark;
- no process/container sandbox because no process execution is exposed.

## Improvement priorities

| Priority | Improvement | Why it matters | Constraint |
|---|---|---|---|
| Done in v0.2 | Mechanical read-only mode | Turns policy into an enforceable capability boundary | Keep only `confirm` and `deny` until more modes are justified |
| Done in v0.2 | Diff-first write approval | Makes the approved change visible | Keep diff bounded; do not store full content in tool-event rows |
| Done in v0.2 | Audit inspection/export | Makes tool behavior reviewable without querying SQLite manually | Preserve local-only defaults |
| Done in v0.2 | Local tool-use evaluation | Reveals model/tool incompatibility early | Clearly label it a smoke test |
| Next | Patch-oriented edit tool | Reduces tokens and accidental whole-file replacement | Must retain preview, approval and path checks |
| Next | Adversarial and multilingual eval cases | Tests denial, traversal, ambiguity, and Chinese prompts | Remain deterministic and quick |
| Later | Streaming with complete audit | Improves interaction latency | Final persisted history must remain complete |
| Later | Explicit read-only MCP allowlist | Enables measured integrations | No implicit discovery or write-capable defaults |

## Project boundary

The project will remain distinctive only if “minimal” is treated as a constraint,
not a temporary lack of features. A new capability should answer three questions:

1. Which measured task cannot be solved with the current surface?
2. What is the smallest permission needed?
3. How will the capability be tested and audited?

If those answers are unclear, the feature belongs in an adapter or a broader agent
platform, not in the core.
