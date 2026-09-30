# v1 compatibility contract

The supported execution model is one active task per database/workspace. Python
hosts and CLI users serialize their runs; the Web server rejects an overlapping
task with HTTP 409 and accepts a new task after completion or failure. Independent
workspaces can use separate databases. This contract does not cover concurrent
processes writing the same session.

## Public Python API

Import `Settings`, `MCPServerSettings`, `AgentRuntime`, `AgentDependencies`,
`ReadTool`, `ModelFactory`, `ContextReducer`, `ContextBudgetExceeded`,
`EventHandler`, `RuntimeEvent`, and `RunOutcome` from `minimal_local_agent`.
The existing imports from `config`, `runtime`, `models`, and `read_tools` remain
available. Documented call signatures are preserved throughout 1.x:

```python
AgentRuntime(settings, *, model_factory=None, read_tools=(), context_reducer=None)
runtime.run(prompt, *, session_id=None, confirm=None, event_handler=None)
ReadTool(name, function, description=None)
Settings.load(config_path=None)
```

`run` is synchronous. It returns a `RunOutcome` with `session_id`, `response`,
`usage`, `receipt_hash`, and `event_handler_errors`. Usage keys come from the model
adapter and may be unavailable. Failed runs raise an exception and leave a failed
run and receipt in the store. Previously saved conversation history stays intact.

Read tools use PydanticAI's `RunContext[AgentDependencies]`. The documented
dependency access is `ctx.deps.session_id` and `ctx.deps.workspace` with
`read_file`, `list_files`, and `search_text`. Other dependency fields are internal
bookkeeping; hosts should not construct `AgentDependencies` themselves. Python
tools and model factories are trusted host code.

`ModelFactory.create(settings)` returns a PydanticAI `Model`. A context reducer
accepts `(messages, max_bytes)` and returns a smaller message sequence. It runs
only on overflow, preserves the latest message object and tool-call/result pairs,
and changes the model view while original messages are retained on successful
runs. The default remains explicit rejection. The budget counts serialized
message bytes, not exact provider tokens.

`RuntimeEvent.to_dict()` and the execution receipt format
`minimal-local-agent.execution-receipt.v1` keep existing fields; additive fields
are permitted. Error text and private helpers are not API contracts. Event-handler
failures are returned in the outcome and do not change task success.

For embedded Web use `create_web_server` or `serve_web` from
`minimal_local_agent.web`. For evaluations use `evaluate_model` and
`load_evaluation_dataset` from `minimal_local_agent.evals`.

## Configuration

Defaults are overridden by TOML, then `MLA_*` environment variables. An explicit
config path (including `MLA_CONFIG`) must exist. Relative workspace/database paths
resolve against the TOML file's directory, or the current directory when no file
is present. The complete field list is in `agent.example.toml`.

Version 1.0 rejects unknown tables/keys and incorrect TOML types. Correct v0.9
configuration remains valid; remove obsolete or misspelled fields that earlier
versions silently ignored. Integer budgets must be positive integers; strings
and booleans are not integer budgets. Keep addresses and key-variable names in
ignored local configuration, and actual secrets in the environment.

## SQLite upgrades

Version 1.0 retains schema 3 used by v0.4–v0.9. It upgrades unversioned v0.1/v0.2
databases and schemas 1/2 to schema 3 without discarding sessions, runs, audit
events, or reversible changes. Existing schema-3 receipts are not rewritten.
Pending schema changes and version markers commit together; a failed upgrade
rolls back. A database from a newer schema is refused before enabling WAL.

Stop active runs and make a SQLite backup before upgrading. The backup API also
includes committed data that has not yet been checkpointed from WAL:

```python
from contextlib import closing
import sqlite3

with closing(sqlite3.connect(".minimal-local-agent/state.db")) as source:
    with closing(sqlite3.connect("state-backup.db")) as backup:
        source.backup(backup)
```

Restore a backup only after stopping all processes using the database. SQLite
history uses PydanticAI messages; custom host message types must remain serializable
by its message adapter. The release checks exercise the oldest supported
PydanticAI version and fresh installations with current compatible dependencies.

## Evaluation data

The input schema remains `minimal-local-agent.eval-dataset.v1`. Its root fields
are `schema`, `name`, and a nonempty `cases` array. Case fields are:

| Field | Contract |
|---|---|
| `name`, `prompt`, `expected_text` | Required nonempty strings; case names are unique |
| `expected_tool` | Optional tool name or null |
| `expected_status` | `ok` (default), `error`, `denied`, or `preview` |
| `family` | Nonempty string, default `general` |
| `forbidden_tools` | Array of tool names; any observed call fails the case |
| `files` | Initial relative-path-to-text map |
| `expected_files` | Exact UTF-8 file-content assertions |
| `absent_files` | Paths that must not exist |

Paths use relative POSIX spelling; absolute paths, drive prefixes, backslashes,
and parent traversal are refused. Unknown fields are errors so a misspelled
assertion cannot silently disappear. The existing v0.5/v0.9 datasets remain valid.
Each case has its own workspace and database and uses the same recorded budgets.

JSON reports use `minimal-local-agent.eval-report.v1` with runtime version,
provider, model, dataset, parameters, per-case results, and task-family summaries.
All attempts count toward latency/tokens per success. Missing or all-zero token
metadata yields null, and no currency cost is inferred. Reports do not add connection
configuration. Model replies and error text should be reviewed before sharing a
report. Additional report fields may be introduced within 1.x.

## Supported release checks

CI verifies Python 3.11 and 3.12 on Linux, Python 3.11 on Windows, the optional MCP
extra, and fresh wheel/source installs on Linux and Windows. The package smoke
check covers the console entry point, embedding API, an audited Python tool,
session history, receipt verification, and packaged Web assets/API without a live
model. Real-model evidence is recorded separately in `VALIDATION.md`.
