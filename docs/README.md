# Documentation

[简体中文](README.zh-CN.md) · [Project overview](../README.md)

Start with the [quick start](../README.md#quick-start), then choose the smallest
guide that answers your question. No separate documentation service is required.

| I want to… | Read |
|---|---|
| Run CLI tasks or use the local Web console | [User guide](GUIDE.md#cli-and-web) |
| Configure a local model or compatible API | [Configuration](GUIDE.md#configuration) |
| Add a Python read tool, model factory, or reducer | [Embedding and extensions](GUIDE.md#embedding-and-extensions), [runnable example](../examples/read_tool.py) |
| Try the interface without touching personal files | [Disposable Web demo](../examples/web_demo.py), [screenshot provenance](assets/README.md) |
| Understand the implementation and trust boundary | [Architecture](ARCHITECTURE.md), [security policy](../SECURITY.md) |
| Upgrade an existing installation or build an integration | [v1 compatibility and migration contract](COMPATIBILITY.md) |
| Evaluate tool use and file assertions | [Evaluations](GUIDE.md#evaluations), [validation records](VALIDATION.md), [reviewed release results](../evals/results/v1.0.json) |
| Compare scope or suggest a feature | [Comparison](COMPARISON.md), [roadmap](ROADMAP.md) |
| Report a bug, contribute, or review a change | [Contributing](../CONTRIBUTING.md), [changelog](../CHANGELOG.md) |

## Reading the evidence

The v1.0 release passed 91 automated tests and small real-model checks on Ollama
and a compatible API. These are regression smoke checks, not a model ranking or a
statistical reliability guarantee. Details, limits, and unknown usage are recorded
in [VALIDATION.md](VALIDATION.md).

Version 1.x keeps the documented extension and data contracts. The roadmap is now
maintenance-focused; automatic compression and broad orchestration are not promises.
