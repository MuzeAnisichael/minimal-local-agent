# v0.2 validation record

Validation date: 2026-08-08.

This file records one development-machine verification run. It is evidence that
the release path worked, not a performance benchmark or a guarantee for other
hardware and model builds.

## Environment

- Windows
- Python 3.11.7
- Ollama 0.32.6
- `qwen3:4b` and `qwen3:8b` installed locally
- Ollama OpenAI-compatible endpoint at `http://localhost:11434/v1`

## Automated gates

```text
ruff format --check .     pass
ruff check .              pass
pytest                    18 passed
python -m pip check       no broken requirements
wheel build               minimal_local_agent-0.2.0-py3-none-any.whl
```

The wheel contained the nine runtime modules, entry-point metadata, package
metadata, and the MIT license.

## Real model/tool integration

Command:

```bash
minimal-agent eval --model qwen3:4b --model qwen3:8b
```

Result:

```text
qwen3:4b: 3/3 passed
  [PASS] list    31538 ms  tools=list_files:ok
  [PASS] read     8797 ms  tools=read_file:ok
  [PASS] search  30416 ms  tools=search_text:ok

qwen3:8b: 3/3 passed
  [PASS] list    26332 ms  tools=list_files:error, list_files:ok
  [PASS] read     7623 ms  tools=read_file:ok
  [PASS] search  17448 ms  tools=search_text:ok
```

The 8B model made one invalid list request, received a bounded tool error, corrected
the request, and then passed. The result therefore demonstrates both direct success
and recoverable tool failure. Latencies include local inference and model loading
effects and should not be used to rank the two models.

## Defects found by running the evaluation

The integration run exposed four issues that unit-only validation had missed:

1. SQLite transaction contexts committed but did not close Windows file handles,
   preventing temporary-directory cleanup. Connections now close explicitly and a
   regression test verifies that the database can be moved after operations.
2. PydanticAI's callable compatibility wrapper triggered a deprecation warning.
   Usage is now accessed through the current property API.
3. A 2048 output-token budget could stop a thinking model before its tool call. The
   evaluation now uses one documented 4096-token budget for every model.
4. Small models misused optional glob parameters (`marker-` and `*`). Model-facing
   list/search schemas no longer expose glob patterns; both recurse by default.

These fixes are examples of the project's intended loop: keep the surface small,
measure real behavior, then remove ambiguity rather than add orchestration.
