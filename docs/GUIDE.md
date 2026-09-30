# User guide

[简体中文](GUIDE.zh-CN.md) · [Documentation](README.md) · [Quick start](../README.md#quick-start)

## CLI and Web

Run from the directory containing your ignored `agent.toml`. Put only permitted
files in the configured workspace; do not point it at your home directory.

```bash
minimal-agent doctor
minimal-agent run --read-only "Summarize the Markdown files"
minimal-agent chat
minimal-agent web
```

The Web console opens at `http://127.0.0.1:8765`. It has a Chinese UI, supports
read-only tasks and non-writing previews, and shows history, events, and receipts.
It cannot approve actual writes. Use interactive CLI for diff-backed approval.
The server binds only to loopback and is not a remotely exposed service.

| Task | Command |
|---|---|
| Preview an edit without applying it | `minimal-agent run --dry-run "Rename the heading in README.md"` |
| Stream JSONL runtime events on stderr | `minimal-agent run --events "Find the release notes"` |
| Inspect effective tools | `minimal-agent capabilities` |
| Browse saved work | `minimal-agent sessions`, `minimal-agent history SESSION_ID` |
| Inspect tool audit | `minimal-agent audit SESSION_ID --json` |
| Inspect reversible edits | `minimal-agent changes`, `minimal-agent change CHANGE_SET_ID` |
| Undo an unchanged applied transaction | `minimal-agent undo CHANGE_SET_ID` |
| Inspect and verify receipts | `minimal-agent receipts SESSION_ID --json`, `minimal-agent verify SESSION_ID --head LAST_RECEIPT_HASH` |

Confirmed writes require an interactive approval over one complete bounded diff;
non-interactive confirmed writes are denied. Undo refuses files changed since the
transaction. Serialize CLI/Python tasks sharing a database/workspace; the Web
server rejects overlapping tasks with HTTP 409. Independent workspaces use separate
databases. See the [supported execution contract](COMPATIBILITY.md).

From a source checkout, for a disposable sample using your configured model, run
`python examples/web_demo.py`. Its workspace and database are temporary; it does
not load personal files or past sessions. Normal exit removes its demo data.
Model requests still go to your configured service and may incur usage charges.

## Configuration

Copy [agent.example.toml](../agent.example.toml) to the ignored `agent.toml`.
Its defaults use Ollama; personal connection settings belong only in that local
file or the environment. Configuration precedence is defaults → TOML → `MLA_*`.
Unknown TOML keys and incorrect types are errors, not silently ignored settings.
Relative workspace/database paths resolve from the configuration directory (or
the current directory if no config file exists). Explicit config paths must exist.

```toml
[agent]
provider = "ollama"
model = "qwen3.5:9b"
base_url = "http://localhost:11434/v1"
request_limit = 6
tool_calls_limit = 8
max_output_tokens = 2048
max_context_bytes = 64000 # serialized message bytes, not tokens
temperature = 0.1
write_policy = "confirm" # "confirm", "preview", or "deny"

[paths]
workspace = "workspace"
database = ".minimal-local-agent/state.db"

[policy.tools]
# search_text = "deny"
# edit_files = "preview"
# count_files = "deny" # An explicitly registered Python read tool.
```

The example file lists every resource limit. Scalar settings have environment
overrides, including `MLA_PROVIDER`, `MLA_MODEL`, `MLA_BASE_URL`, `MLA_API_KEY_ENV`,
`MLA_WORKSPACE`, `MLA_DATABASE`, `MLA_WRITE_POLICY`, and each `MLA_MAX_*` budget.
`MLA_DISABLED_TOOLS` removes named capabilities; `MLA_CONFIG` selects a config file.
Write tools accept `ask`, `preview`, or `deny`, never approval-free `allow`. Read
and external-read tools accept `allow` or `deny`.

### Other model endpoints

A local server, official API, or relay must implement OpenAI Chat Completions
and compatible tool calls. Change only your ignored configuration:

```toml
[agent]
provider = "openai-compatible"
model = "your-tool-capable-model"
base_url = "https://your-provider.example/v1"
api_key_env = "MODEL_API_KEY"
```

Set the actual key in the local `MODEL_API_KEY` environment variable.
`api_key_env` is its name, not the secret. Keyless local compatible servers may
omit it and use loopback/private-network URLs. Remote endpoints require a key;
keyed endpoints outside loopback require HTTPS.

`doctor` checks `/models` without a generation call. Endpoints without that route
can remain "unverified" until a task succeeds. This is a protocol adapter, not a
guarantee for every provider-specific feature or model. Remote services receive
prompts and tool results, including workspace content read by the agent.

### Context budget

`max_context_bytes` checks UTF-8 JSON message size before every model request,
including requests after tool results. It excludes tool schemas and provider
framing: it is not exact token counting or a guarantee of context-window fit.
Overflow fails visibly, records size/hash in the receipt, and preserves saved
conversation history. Start a new session or adjust your local budget.

An explicit trusted-host `context_reducer` may return a smaller model view on
overflow. It must keep the latest message unchanged, preserve tool-call/result
pairs, and fit the budget. Reduced-view hashes are recorded; full original history
is retained on successful runs. No automatic summarizer or TOML code import exists.

## Embedding and extensions

```python
from minimal_local_agent import AgentRuntime, Settings

runtime = AgentRuntime(Settings.load("agent.toml"))
outcome = runtime.run(
    "Summarize fact.txt",
    event_handler=lambda event: print(event.to_dict()),
)
print(outcome.response)
print(outcome.receipt_hash)
```

Use `read_tools=(ReadTool(...),)` for reviewed Python read functions,
`model_factory=` for model construction, and `context_reducer=` for host-owned
context reduction. See [examples/read_tool.py](../examples/read_tool.py) and the
[v1 API contract](COMPATIBILITY.md#public-python-api). For Web embedding, import
`serve_web` from `minimal_local_agent.web` and pass `read_tools=` explicitly.

Denied read tools disappear from the model schema. Calls use the existing external
result limit and record argument names/hashes and result size/hash, not full
arguments/results, in audit and receipts. Conversation history may contain full
results. `ReadTool` declares trusted read-only code; it is not a Python sandbox.
The removed raw `toolsets`/`external_tools` escape hatch is not a supported host API.

Events include `run.started`, `tool.completed`, `mutation.preview`,
`mutation.applied`, `context.rejected`, `context.reduced`, `run.completed`, and
`run.failed`. Observer errors are returned in `outcome.event_handler_errors`
without changing task success.

Receipts contain endpoint/prompt/response hashes, usage, capabilities, and
audit-safe tool metadata in a per-session hash chain. Internal verification detects
broken links or edits without recomputed hashes. Keep the final hash outside
SQLite and use `verify --head` to detect a recomputed or truncated chain. Without
that external head, a database writer can recompute it. This is not identity or
remote attestation. History and undo snapshots can contain sensitive file text.

### Optional MCP client

```bash
python -m pip install -e ".[mcp]"
```

```toml
[[mcp.servers]]
name = "notes"
url = "http://127.0.0.1:8000/mcp"
allow_tools = ["search_notes", "read_note"]

[policy.tools]
mcp_notes_read_note = "deny"
```

Only explicitly named tools are exposed, prefixed like `mcp_notes_search_notes`.
MCP accepts loopback HTTP(S) endpoints only: `localhost`, `127.0.0.1`, or `::1`.
Server instructions, sampling, elicitation, filesystem roots, implicit tool
exposure, and remote URLs are disabled; results are bounded. An allowlist is the
operator's assertion of read-only behavior, not a sandbox or server annotation
guarantee. Connect only reviewed, trusted servers.

## Evaluations

```bash
minimal-agent eval --model YOUR_TOOL_CAPABLE_MODEL --json
minimal-agent eval --dataset evals/adversarial.json --model YOUR_TOOL_CAPABLE_MODEL --json
```

The built-in dataset checks list/read/search. External datasets use
`minimal-local-agent.eval-dataset.v1` with isolated fixtures, expected
tool/status/answer, forbidden tools, task families, exact `expected_files`, and
`absent_files`. Assertions inspect real UTF-8 file bytes, not model self-grading.
Unknown fields and unsafe/non-POSIX fixture paths are rejected before model calls.

Reports use `minimal-local-agent.eval-report.v1`, recording runtime, provider,
model, fixed budgets, cases, and families. All attempts count toward latency and
tokens per success. Missing/all-zero usage yields `null` / `n/a`; no money price
is inferred. Review replies and errors before sharing full reports. See
[format contracts](COMPATIBILITY.md#evaluation-data), [validation](VALIDATION.md),
and [reviewed v1.0 results](../evals/results/v1.0.json).

## Troubleshooting

- **Model not found:** pull a tool-capable model and set its exact local name.
  The model used in screenshots is a demo choice, not the public default.
- **Connection unverified:** check your own base URL, `/models` support, and
  key-variable name; try one read-only task. Never paste a key into an issue.
- **Configuration error:** compare field spelling and types with the example;
  do not quote integer budgets or use booleans as integers.
- **Context rejected:** start a new session or change the local byte budget;
  automatic compression is intentionally off.
- **HTTP 409:** wait for the current Web task to finish before submitting another.
- **Write not applied:** Web preview and CLI dry run never write. Use interactive
  CLI approval only if you intend to apply the complete diff.
- **Upgrade needed:** stop runs, make a SQLite backup, and follow
  [migration instructions](COMPATIBILITY.md#sqlite-upgrades). Never downgrade a
  database with a newer schema.
