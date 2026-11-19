# 配置

配置文件是 TOML，默认读取当前目录的 `agent.toml`，可用 `mla --config 路径` 指定。
从 [`agent.example.toml`](../agent.example.toml) 复制一份开始；`agent.toml` 已被 Git 忽略。

- 所有配置项都有默认值，什么都不写也能运行（使用本地 Ollama）。
- 未知字段会直接报错，拼错的配置不会被悄悄忽略。
- 相对路径按配置文件所在目录解析。
- 密钥只从环境变量读取（`api_key_env`），不会写进配置文件或状态库。

## `[workspace]`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `root` | `"workspace"` | 文件工具能访问的目录。绝对路径、`..`、指向外部的符号链接都会被拒绝 |
| `state_dir` | `".mla"` | 状态库目录，库文件是 `state.db`（任务、事件、检查点、工具账本、记忆） |

## `[models.<名字>]`

可以定义任意多个模型，名字供 `[routing]` 和 `mla eval --model` 引用。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `provider` | `"ollama"` | `ollama` 或 `openai-compatible`（DeepSeek、OpenRouter、vLLM 等） |
| `model` | `"qwen3.5:9b"` | 服务端的模型名 |
| `base_url` | `"http://localhost:11434/v1"` | OpenAI 兼容接口地址 |
| `api_key_env` | 无 | 存放密钥的环境变量名 |
| `temperature` | `0.1` | 0 到 2 |
| `input_price_per_mtok` / `output_price_per_mtok` | 无 | 每百万 token 的价格（美元）。配置后才计算成本，否则成本记为未知 |

## `[routing]`

按角色选模型，每个角色是一条模型链，前一个请求失败（异常）就换下一个，失败记为 `model.failed` 事件。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `plan` | `["default"]` | 生成计划 |
| `act` | `["default"]` | 执行步骤、调用工具 |
| `summarize` | `["default"]` | 上下文压缩时做摘要（`context.compaction = "summarize"`） |

没写 `[routing]` 且没有名为 `default` 的模型时，所有角色都用配置里的第一个模型。

## `[budget]`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `requests` | `16` | 模型请求次数上限 |
| `tool_calls` | `32` | 工具调用次数上限 |
| `output_tokens` | `16000` | 输出 token 总量上限 |
| `cost_usd` | 无 | 成本上限（需要配置价格） |
| `max_output_tokens_per_request` | `2048` | 单次请求的输出上限；实际取它和剩余额度中较小的值 |

子任务从父任务预算里预留额度，结束后按实际用量结算回父任务。

## `[context]`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `max_tokens` | `24000` | 估算的上下文超过这个值时压缩 |
| `compaction` | `"truncate"` | `truncate` 只列出被丢弃的工具调用；`summarize` 交给 summarize 模型写摘要 |
| `keep_recent_steps` | `4` | 压缩时保留最近几个步骤 |

## `[tools]`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `timeout_s` | `30` | 单次工具调用超时（秒） |
| `max_file_bytes` | `200000` | 读取单个文件的大小上限 |
| `max_list_results` / `max_search_results` / `max_search_files` | `200` / `100` / `500` | 列目录和搜索的结果上限 |
| `max_change_files` | `8` | 一个变更集最多改几个文件 |
| `max_result_chars` | `20000` | 返回给模型的工具结果最长字符数 |

## `[policy]`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `write` | `"ask"` | 写工具的默认决定：`allow` 直接执行、`ask` 显示 diff 并询问、`preview` 只显示 diff 不写入、`deny` 禁止 |
| `tools` | `{}` | 按工具名单独设置，例如 `search_text = "deny"`。被拒绝的工具不会出现在模型可见的工具列表里。全局 `write` 为 `deny` 或 `preview` 时是硬上限，单个工具不能放宽；读工具只能设为 `deny` |

## `[planning]`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否先生成结构化计划 |
| `max_replans` | `2` | 校验失败后最多带着失败原因重新执行几轮 |

## `[recovery]`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用恢复（评测里的“基础循环”就是关闭） |
| `tool_retries` | `1` | 工具超时后自动重试的次数 |
| `compensate_on_failure` | `true` | 任务失败或取消时是否自动撤销本任务写过的文件 |

## `[memory]`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `strategy` | `"none"` | `none` 不跨任务保留信息；`episodic` 记录每个任务的经验，新任务按关键词召回 |
| `max_notes` | `5` | 每次最多召回几条经验 |

## `[[mcp.servers]]`

需要安装 MCP 依赖：`pip install -e ".[mcp]"`。

```toml
[[mcp.servers]]
name = "notes"
command = ["python", "notes_server.py"]   # stdio；或 url = "http://127.0.0.1:8000/mcp"
tools = { search_notes = "read", add_note = "write" }
idempotent = ["add_note"]
```

| 字段 | 说明 |
| --- | --- |
| `name` | 服务器名，工具在模型那里叫 `mcp_<name>_<工具>` |
| `command` / `url` | 二选一：stdio 启动命令，或 Streamable HTTP 地址 |
| `tools` | 白名单，值是 `read` 或 `write`。只暴露这里列出的工具，启动时检查它们确实存在 |
| `idempotent` | 声明为幂等的写工具。MCP 写操作无法对账，只有列在这里的工具才会在中断后重放 |
| `timeout_s` | 覆盖该服务器的工具超时 |
