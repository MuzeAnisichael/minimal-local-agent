# 使用指南

[English](GUIDE.md) · [文档导航](README.zh-CN.md) · [快速开始](../README.zh-CN.md#快速开始)

## CLI 与 Web

在被 Git 忽略的 `agent.toml` 所在目录运行。只把允许访问的文件放入配置工作区，
不要把用户主目录或包含密钥的宽泛目录当作工作区。

```bash
minimal-agent doctor
minimal-agent run --read-only "总结工作区中的 Markdown 文件"
minimal-agent chat
minimal-agent web
```

Web 地址默认为 `http://127.0.0.1:8765`，目前是中文界面，支持只读任务、不落盘的变更
预览、历史、事件和凭证。Web 不能批准真实写入；需要看完整 diff 并确认落盘时使用
交互式 CLI。服务只监听回环地址，不应作为远程服务暴露。

| 目标 | 命令 |
|---|---|
| 预览修改，不应用 | `minimal-agent run --dry-run "修改 README.md 标题"` |
| 在 stderr 输出 JSONL 事件 | `minimal-agent run --events "查找发布说明"` |
| 查看实际开放的工具 | `minimal-agent capabilities` |
| 查看历史 | `minimal-agent sessions`、`minimal-agent history SESSION_ID` |
| 查看工具审计 | `minimal-agent audit SESSION_ID --json` |
| 查看可撤销变更 | `minimal-agent changes`、`minimal-agent change CHANGE_SET_ID` |
| 撤销未被再次修改的事务 | `minimal-agent undo CHANGE_SET_ID` |
| 查看、验证凭证 | `minimal-agent receipts SESSION_ID --json`、`minimal-agent verify SESSION_ID --head LAST_RECEIPT_HASH` |

确认式写入对完整且有上限的 diff 请求一次批准，非交互式确认写入会被拒绝。
若文件在事务之后被修改，撤销会拒绝覆盖。共享工作区/数据库的 CLI/Python 任务须串行；
重叠 Web 任务返回 HTTP 409，独立工作区使用独立数据库。

在源码目录想体验界面而不碰个人文件，可运行 `python examples/web_demo.py`。它使用你的模型配置，
但只加载公开示例和临时数据库，不加载个人文件或历史会话；正常退出会清理演示数据。
真实请求仍发送给配置的模型服务，可能产生调用费用。

## 配置

复制 [agent.example.toml](../agent.example.toml) 为本地忽略文件 `agent.toml`。
公开默认值为 Ollama，个人连接信息仅放本地文件或环境变量。
优先级为默认值 → TOML → `MLA_*`；未知字段、错误类型会报错，不会被静默忽略。
相对路径以配置文件目录为基准，无配置时以当前目录为基准；显式指定的配置必须存在。

```toml
[agent]
provider = "ollama"
model = "qwen3.5:9b"
base_url = "http://localhost:11434/v1"
request_limit = 6
tool_calls_limit = 8
max_output_tokens = 2048
max_context_bytes = 64000 # 序列化消息字节数，不是 token 数
temperature = 0.1
write_policy = "confirm" # "confirm"、"preview" 或 "deny"

[paths]
workspace = "workspace"
database = ".minimal-local-agent/state.db"

[policy.tools]
# search_text = "deny"
# edit_files = "preview"
# count_files = "deny" # 已显式注册的 Python 只读工具
```

示例配置列出了全部资源上限。标量项支持环境变量覆盖，常用项包括 `MLA_PROVIDER`、
`MLA_MODEL`、`MLA_BASE_URL`、`MLA_API_KEY_ENV`、`MLA_WORKSPACE`、`MLA_DATABASE`、
`MLA_WRITE_POLICY` 和各个 `MLA_MAX_*`。`MLA_DISABLED_TOOLS` 移除指定能力，
`MLA_CONFIG` 指定配置路径。写工具只能是 `ask`、`preview`、`deny`，不能绕过批准；
只读和外部只读工具只能是 `allow`、`deny`。

### 其他模型服务

本地服务器、官方 API 或中转站须支持 OpenAI Chat Completions 及兼容工具调用。
只修改被 Git 忽略的配置：

```toml
[agent]
provider = "openai-compatible"
model = "your-tool-capable-model"
base_url = "https://your-provider.example/v1"
api_key_env = "MODEL_API_KEY"
```

实际密钥保存在本机 `MODEL_API_KEY` 环境变量中，`api_key_env` 是变量名称，不是密钥。
无密钥的本地兼容服务可以省略它并使用回环/私有网络地址。远程端点需要密钥，带密钥的
非回环端点必须使用 HTTPS。

`doctor` 探测 `/models`，不发起生成请求；没有模型列表接口的服务可能显示“未验证”，
直到任务成功。适配器不保证所有模型或服务商扩展都可用。远程服务会接收提示词与工具
结果，包括 Agent 读到的工作区内容。

### 上下文预算

`max_context_bytes` 在每次请求前检查 UTF-8 JSON 消息大小，包括工具返回后的请求。
工具 schema 和服务商封装不计入，因此它不是精确 token 计数，也不保证适配上下文窗口。
超限时明确失败，凭证记录大小与哈希，已保存的完整会话不会被裁剪。可开始新会话或调整
本地预算。

可信宿主可显式传入 `context_reducer`，超限时返回更小的模型视图；必须保持最新消息不变，
保留工具调用/结果配对，并满足预算。缩减视图的哈希可审计，成功运行仍保存原始完整历史。
没有默认自动摘要，也不会从 TOML 动态导入代码。

## 嵌入与扩展

```python
from minimal_local_agent import AgentRuntime, Settings

runtime = AgentRuntime(Settings.load("agent.toml"))
outcome = runtime.run(
    "总结 fact.txt",
    event_handler=lambda event: print(event.to_dict()),
)
print(outcome.response)
print(outcome.receipt_hash)
```

用 `read_tools=(ReadTool(...),)` 注册已审阅的 Python 只读函数，用 `model_factory=`
替换模型构造，用 `context_reducer=` 接入宿主缩减策略。完整示例见
[examples/read_tool.py](../examples/read_tool.py)，稳定签名见
[v1 约定（英文）](COMPATIBILITY.md#public-python-api)。嵌入 Web 时从
`minimal_local_agent.web` 导入 `serve_web`，显式传入 `read_tools=`。

拒绝的工具不会进入模型 schema。调用受现有外部结果上限约束，审计/凭证只存参数名/哈希
和结果大小/哈希，不存完整参数/结果；会话历史仍可能包含完整结果。`ReadTool` 是可信代码
的只读声明，不是 Python 沙箱。旧的原始 `toolsets`/`external_tools` 入口不再属于宿主 API。

事件包括开始、工具完成、变更预览/应用、上下文拒绝/缩减和运行成功/失败。
观察回调出错不会改变任务结果，错误返回在 `outcome.event_handler_errors` 中。

凭证包含端点/提示词/回复哈希、用量、能力清单和审计元数据，按会话组成哈希链。
内部校验发现断链或未重算哈希的修改；将最终哈希保存在 SQLite 之外并用 `verify --head`
可发现整链重算或截断。没有外部链头时，数据库写入者能重算整链。凭证不是数字身份或
远程证明；历史和撤销快照可能保存敏感文件文本，应妥善保护。

### 可选 MCP

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

只有显式列出的工具会开放，带 `mcp_notes_search_notes` 这样的前缀。
仅接受 `localhost`、`127.0.0.1`、`::1` 的 HTTP(S) 回环服务。服务端指令、采样、交互请求、
文件系统根目录、隐式工具和远程地址关闭，结果有上限。白名单是操作者确认只读行为的
声明，不是沙箱或服务端注解保证；只连接已审阅的可信服务。

## 评测

```bash
minimal-agent eval --model YOUR_TOOL_CAPABLE_MODEL --json
minimal-agent eval --dataset evals/adversarial.json --model YOUR_TOOL_CAPABLE_MODEL --json
```

内置评测检查 list/read/search。外部格式为 `minimal-local-agent.eval-dataset.v1`，支持隔离
初始文件、预期工具/状态/回答、禁止工具、任务族、精确内容 `expected_files` 和不存在路径
`absent_files`。产物检查读取真实 UTF-8 字节，不依赖模型自评。未知字段和不安全/非 POSIX
路径在模型调用前报错。

报告采用 `minimal-local-agent.eval-report.v1`，记录版本、服务类型、模型、固定预算、用例和
任务族。每成功任务耗时/token 包含全部尝试。缺失或全零用量显示未知，不推算货币价格。
分享完整报告前检查回答与错误文本。详见[格式约定（英文）](COMPATIBILITY.md#evaluation-data)、
[验证记录（英文）](VALIDATION.md) 和[发行摘要](../evals/results/v1.0.json)。

## 常见问题

- **找不到模型：**拉取支持工具调用的模型，并配置准确名称。截图模型是演示选择，
  不是公开默认值。
- **连接未验证：**检查你自己的地址、`/models` 支持和密钥变量名，再尝试一次只读任务。
  不要把密钥贴进 Issue。
- **配置报错：**核对示例里的字段和类型，整数预算不要加引号或填布尔值。
- **上下文超限：**开启新会话或调整本地字节预算，自动压缩默认关闭。
- **HTTP 409：**等当前 Web 任务结束再提交。
- **修改没有落盘：**Web 预览和 CLI dry run 都不写文件，只有明确想应用完整 diff 时
  才使用交互式 CLI 批准。
- **升级已有状态：**停止运行、备份 SQLite，参考[迁移说明（英文）](COMPATIBILITY.md#sqlite-upgrades)。
  不要降级更新版本的数据库。
