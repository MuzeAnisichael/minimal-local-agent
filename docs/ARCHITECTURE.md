# 架构说明

本文说明 2.0 的内核设计：运行时怎样掌握任务循环、怎样保证工具调用幂等、中断后怎样恢复、事件轨迹怎样支持回溯，
以及评测怎样在固定模型和预算下对比策略。

## 设计原则

1. **运行时掌握循环。** 规划、执行、校验是运行时自己的状态机，不交给框架的 Agent 循环。
   PydanticAI 只在 `model/adapter.py` 里用于发起一次模型请求（`Model.request`），换掉它只需改这一层。
2. **状态库是唯一事实来源。** 任务、步骤、事件、检查点、工具账本、记忆都在一个 SQLite 文件里（WAL 模式）。
   进程里不保存任何恢复所必需、而状态库里没有的东西。
3. **所有工具走同一个入口。** 内置文件工具、MCP 工具、子任务、控制工具都用 `Tool` 描述，
   由 `ToolExecutor` 统一处理参数校验、权限、审批、预算、超时、取消、重试、账本和事件。
4. **副作用先落账，再落地。** 写工具分两步：`prepare` 只计算变更（可序列化的 `Prepared`），
   执行入口把它写进账本后，再交给 `EffectHandler.apply` 落地。恢复和补偿只依赖账本里的数据。

## 模块

```
src/minimal_local_agent/
├── config.py            配置模型（严格校验，未知字段报错）
├── policy.py            工具权限：allow / ask / preview / deny
├── cli.py               mla 命令行
├── core/                与 I/O 无关的核心逻辑
│   ├── loop.py          TaskRunner：plan → act → verify 状态机，TaskState 检查点
│   ├── plan.py          计划格式、解析、update_plan 控制工具
│   ├── budget.py        用量与预算（含子任务预留和结算）
│   └── context.py       token 估算、按步骤压缩上下文、消息序列化
├── runtime/             把核心逻辑和外部世界连起来
│   ├── runtime.py       AgentRuntime：组装存储、模型路由、工具，提供 run / resume / cancel / compensate
│   ├── executor.py      ToolExecutor：统一工具执行入口与账本对账
│   ├── store.py         SQLite 状态库
│   ├── context.py       Tracer（事件）、CancelToken（取消）、ToolContext
│   ├── recovery.py      逆序补偿
│   └── trace.py         事件树与产物回溯
├── model/
│   ├── adapter.py       模型构建、ModelRouter（按角色选模型、失败回退、计费）
│   └── scripted.py      脚本模型：测试与离线评测用
├── tools/
│   ├── registry.py      Tool / Prepared / EffectHandler 协议、ToolRegistry
│   ├── fs.py            工作区：路径越界防护、原子写入
│   ├── files.py         文件变更集：prepare / probe / apply / compensate
│   ├── workspace.py     内置文件工具
│   ├── subtask.py       run_subtask
│   └── mcp.py           MCP 连接器（官方 mcp SDK）
├── verify/checks.py     产物校验
├── memory/strategies.py 记忆策略：none / episodic
└── eval/
    ├── faults.py        故障注入
    └── runner.py        评测运行器与报告
```

## 任务状态机

```
          ┌──────── 校验失败且还能重新规划 ────────┐
          ▼                                        │
 plan ──▶ act ──(模型给出最终回答)──▶ verify ──────┤
          │  ▲                                     │
          └──┘ 模型要求调用工具：执行后回到 act      └─▶ completed
                                                       failed（预算耗尽 / 模型全部失败 / 校验未通过 / 上下文溢出）
                                                       cancelled
```

- **plan**：召回记忆（子任务不召回），请求规划模型生成计划，拼出开头提示（目标、经验、计划、校验规则）。
- **act**：每一轮是一个“步骤”。先检查取消和预算，必要时压缩上下文，然后请求 act 模型。
  模型响应写入检查点之后才执行工具；工具结果追加进消息后再写一次检查点。
- **verify**：运行校验。失败时若 `replans < max_replans`，把失败原因作为新消息追加，回到 act；否则任务失败。
- **结束**：状态不是 completed 时自动补偿（可在 `recovery` 里关闭），然后记录记忆（子任务不记录）。

检查点保存 `TaskState`：阶段、消息（序列化）、计划、步骤序号、重新规划次数、回答、预算用量、根事件。
恢复时读取最新检查点，若最后一条消息是带工具调用的模型响应，就继续执行这些工具调用（凭账本去重）。

## 工具执行与幂等账本

每次工具调用的幂等键是 `digest([task_id, step_id, call_id, tool, args])`，在账本 `tool_ledger` 里有一行：

```
pending ──▶ applying（已写入 prepared 数据）──▶ done
                                         ├──▶ failed
                                         └──▶ compensated（已撤销）
```

执行入口的流程：

1. 校验参数（自己解析 JSON，必须是对象）、检查工具是否存在、是否被策略禁止。
2. 查账本：`done` / `failed` 直接重放结果，不重复执行也不重复计费；`applying` 进入对账。
3. 只读工具：在 `guarded()` 里运行（同时等待取消信号和超时），超时按 `recovery.tool_retries` 重试。
4. 写工具：`prepare`（同样可超时重试，因为不落盘）→ 预览 / 审批 → 账本标记 `applying` 并保存 prepared 数据 → `apply`。

对账（中断后遇到 `applying`）由 `EffectHandler.probe` 判断外部状态：

| probe 结果 | 处理 |
| --- | --- |
| after（已写入） | 直接记为完成 |
| before（未写入）/ partial（部分写入） | 重新 apply（文件变更集会先回滚到一致状态） |
| conflict（文件被别人改过） | 报冲突，不覆盖 |
| unknown（外部系统无法对账，如 MCP） | 工具声明了 `replay_safe`（配置里的 `idempotent`）才重放，否则报“结果未知” |

文件变更集记录每个文件的写前、写后内容和哈希。`apply` 逐个写入，任何一个失败就回滚已写的；
写之前若发现文件当前内容既不是写前也不是写后，就是外部改动，抛 `FileConflictError`。

## 补偿

任务失败或取消时，`compensate_task` 按账本逆序处理本任务和所有子任务的 `done` 写操作：
文件当前内容仍是本任务写入的结果才恢复写前内容，否则记为冲突（`compensation.conflict`）留给人处理；
外部 MCP 写操作无法补偿，记为 `compensation.skipped`。补偿完成的行标记为 `compensated`，重复补偿不会重复执行。

## 取消与超时

`CancelToken` 同时看进程内的事件和状态库里的 `cancel_requested` 标记，所以 `mla cancel` 能取消另一个进程里的任务；
取消父任务会级联取消子任务。模型请求和工具执行都在 `guarded()` 里与取消信号、超时竞争。

## 预算与子任务

`Budget` 是一本账：请求数、工具调用数、输出 token、成本四项上限，每次模型请求前按剩余额度限制 `max_tokens`。
`run_subtask` 从父预算里预留子任务额度（`max_requests` 等参数），子任务结束后把实际用量结算回父任务、释放预留。
子任务的工具必须是父任务可见工具的子集，默认只读；子任务看不到 `run_subtask`，不能再派生。
子任务 ID 由父任务 ID 和调用的幂等键决定，父任务恢复时会找到同一个子任务并继续它。

## 上下文预算

按 token 估算（ASCII 约 4 字符一个 token，其他字符一个）判断是否超出 `context.max_tokens`。
压缩以“步骤”为单位丢弃最早的历史（一个模型响应和它对应的工具结果不会被拆开），保留最近 `keep_recent_steps` 步；
被丢弃的内容合并成一条摘要（`truncate` 只列出工具调用，`summarize` 交给 summarize 模型），并附上当前计划。
多次压缩时合并已有摘要，不会层层叠加。

## 模型路由

`routing` 为 plan / act / summarize 三种角色各配一个模型链，前一个请求失败（异常）就换下一个，失败记为 `model.failed` 事件。
配置价格后按 token 计费；服务端没返回用量时成本记为未知，不当成 0。

## 事件与回溯

`events` 表的每条事件有 `parent_id`，形成一棵树：

```
task.started
├── model.called (plan) ── plan.created
├── step.started
│   └── model.called (act)
│       └── tool.called ── tool.completed ── artifact.written（带 artifact_path / artifact_sha）
├── verify.passed ── artifact.verified
└── task.completed
```

事件还记录输入和输出的哈希（`input_sha` / `output_sha`）。`mla trace --artifact 路径` 找到最近一次写出该文件的
`artifact.written`，沿 `parent_id` 走到根：产物 → 工具调用 → 模型调用（含当时的输入哈希）→ 步骤 → 任务目标。

## MCP

`MCPConnector` 用官方 `mcp` SDK 连接 stdio（`command`）或 Streamable HTTP（`url`）服务器，
启动时检查白名单里的工具确实存在。工具名是 `mcp_<服务器>_<工具>`，按配置标注 read / write；
写工具和内置写工具一样经账本执行，effect 类型是 `mcp:<服务器>`。连接器在运行时里长期存在，
每次 `asyncio.run` 结束时断开、下次使用时重连，工具和 effect 通过服务器名找到当前会话。

## 记忆

- `none`：不跨任务保留任何东西。
- `episodic`：任务结束后记一条经验（目标、回答或失败原因），新任务开始时按关键词重叠召回最相关的几条，
  作为“仅供参考、可能过时”的内容放进开头提示。经验按工作区隔离。

## 评测

评测集按任务族组织（`evals/families/*.json`），每个用例包含目标、初始文件、校验规则、离线脚本和可选的故障。
四种策略是配置覆盖（见 `eval/runner.py` 的 `STRATEGIES`）：恢复开关、重新规划次数、记忆策略，模型和预算对所有策略相同。

运行方式：

- 同一任务族的用例按顺序在同一个状态库里运行，记忆策略能用上前面用例留下的经验；每个用例开始前重置工作区。
- 崩溃类故障（`SimulatedCrash` 继承 `BaseException`，模拟进程被杀）：恢复关闭时任务直接算失败；
  恢复开启时用全新的运行时（不再注入故障）从检查点续跑，相当于进程重启。
- 其他故障（超时、外部改动、非法调用）由运行时在任务内部处理，效果取决于重试和重新规划是否开启。

指标：

- **成功率** = 通过全部校验的用例 / 用例数
- **故障恢复率** = 注入故障的用例中最终成功的比例
- **每成功任务成本 / tokens** = 所有用例（含失败）的花费之和 / 成功用例数

离线模式下模型行为由脚本决定，评测验证的是运行时机制是否真的起作用，并作为 CI 的回归测试；
脚本的 `variants` 用来模拟“看到相关经验就少走几步”的模型，使记忆策略的效果可测。
要比较真实模型下的策略差异，去掉 `--offline` 运行。

## 与 1.x 的关系

2.0 重写了执行内核（循环、执行入口、状态库、恢复、评测），复用了 1.x 中工作区防护、原子写入和文件变更集等叶子模块。
去掉了网页控制台、检查器和 A2A，回溯改用 `mla trace`。代码量从约 7000 行降到约 4700 行（含评测）。
