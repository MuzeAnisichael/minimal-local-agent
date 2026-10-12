# 可选模块与运行观察

[文档导航](README.zh-CN.md) · [基础架构](ARCHITECTURE.md) · [路线图](ROADMAP.zh-CN.md)

v1.1.0 是本地尚未发布的模块化扩展。默认仍为一个有预算和权限边界的 Agent；
记忆、规划和子任务均需显式选择，不会自动启动团队、下载模型或发现其他 Agent 服务。
本页说明模块接口与用法，基础工具、编辑事务和审计机制仍见[架构](ARCHITECTURE.md)。

## 模块边界

| 模块 | 宿主接口 | 默认实现与边界 |
|---|---|---|
| 运行内核 | `AgentRuntime` | PydanticAI 模型/工具循环；权限、共享预算、会话和执行回执 |
| 长期与实体记忆 | `MemoryStore` / `memory_store=` | 本地 SQLite、作用域隔离、字面词检索；只有批准条目可参与检索 |
| 规划 | `Planner` / `planner=` | `ModelPlanner` 单次生成有完成条件的建议，不执行步骤 |
| 子任务 | `Peer` / `peers={name: peer}` | 显式可信对端；`LocalPeer` 或可选 `A2APeer`，不复制完整会话/记忆 |
| 观察 | `EventHandler` / `ObservationHub` | 只读事件与快照；观察失败不改变任务执行结果 |

宿主可替换接口实现，不需要修改主循环。模块由 Python 对象显式注入，没有配置驱动的
任意代码加载或通用插件注册平台。宿主代码与本机对端是可信组件，不是 Python 沙箱。

## 开启记忆和规划

在自己的、被 Git 忽略的 `agent.toml` 中选择模块：

```toml
[memory]
enabled = false
# database = ".minimal-local-agent/memory.db"
# scope = "my-project"

[planner]
enabled = false
```

未指定记忆库时，使用运行数据库旁的 `memory.db`；未指定作用域时，使用工作区路径
派生的标识，而不是会话 ID。同一作用域允许跨会话检索；不同作用域不会互相检索。
在同一记忆库中设置相同的显式 `scope` 是主动共享记忆，不应随意复用。

可只为当前入口启用，不修改默认配置：

```bash
minimal-agent run --read-only --memory "检索 UTF-8 相关的已批准记忆"
minimal-agent chat --memory --planner
minimal-agent web --memory --planner
```

这些任务仍使用已配置的模型，可能产生模型调用费用。规划和记忆不是上下文压缩器；
自动压缩仍默认关闭，由宿主单独提供 `ContextReducer`。

## 候选记忆和人工审核

会话历史与知识记忆分开保存。Agent 只能检索已批准知识或提出候选，不能自行批准。
候选的内容与来源保留在本地，批准前不注入下一轮的长期知识上下文。

记忆管理 CLI 不构造模型；运行 `memory` 命令本身就是显式管理选择，不要求
`[memory].enabled = true`。但 Agent 要使用这些记录，仍需 `--memory` 或 TOML 开关。

```bash
minimal-agent memory propose "项目文本文件统一使用 UTF-8" --source "human:project"
minimal-agent memory list --status candidate
minimal-agent memory show RECORD_ID
minimal-agent memory approve RECORD_ID --revision REVISION
```

把 `RECORD_ID`、`REVISION` 换为 `show` 中刚审核的 ID 和版本。更新、批准、拒绝、忘记
都必须带 `--revision`；记录已变化时会拒绝陈旧操作。每个子命令都支持 `--json`。

| 命令 | 效果 |
|---|---|
| `list` / `show` | 查看当前作用域；`list` 支持 `--status`、`--limit 1..100` |
| `propose` | 新建未生效候选，可带 `--source` |
| `update RECORD_ID CONTENT --revision N` | 针对已批准条目提出更新候选，不原地激活变更 |
| `approve RECORD_ID --revision N` | 明确批准；更新候选批准后替代旧条目 |
| `reject RECORD_ID --revision N` | 拒绝候选，不加入检索 |
| `forget RECORD_ID --revision N` | 排除未来检索，不删除会话、审计或旧文件快照 |

检索使用有界字面词匹配，不增加 embedding 服务、向量库或额外模型调用。
每次注入只选择少量已批准记录并受字节预算约束；这些内容是参考资料，不是系统权限
或已完成工作的证据。文件只读模式也会保存历史/审计；启用记忆后，还可能保存候选。

## 实体记忆

实体是当前作用域内的明确 `entity_key` 和字符串属性，没有默认遍历历史、自动入库的
实体抽取流水线，也不是知识图谱：

```bash
minimal-agent memory propose "项目使用 Python 3.11+" --entity project \
  --attribute language=Python --attribute min_python=3.11 --source "human:project"
```

上面的换行续写适用于 Bash；PowerShell 可写成一行。`--attribute KEY=VALUE` 可重复。
实体仍走候选、审核与版本检查。`update` 未提供属性时保留原属性；一旦提供属性，
会用整组替换，而不是悄悄合并。存储层不自动补全属性或生成关系图，模型提出的候选
仍需人工核对。

## 建议型独立规划器

`ModelPlanner` 在任务开始时生成一次小计划，每步有可观察的完成条件。它没有任务执行工具，
不执行步骤、不把计划标为已完成，也不提供 DAG、自动重规划或补偿工作流。
执行循环可以参考计划，实际完成证据仍来自工具结果与产物。

规划调用和输出计入同一轮的共享预算，不另开无上限的“规划预算”。须为规划和主执行
各保留至少一次请求；预算不足或计划生成失败会终止本轮，不伪造计划或默默降级。
自定义 `Planner` 同样必须遵守接口分配的预算并报告用量；未报告 token 数保持未知。

## 显式本地子任务

`AgentRuntime(..., peers={"reader": peer})` 才会开放相应委派工具，最多四个显式对端；
不从任务或配置自动发现 Agent。`LocalPeer` 要使用与父运行不同的工作区和审计数据库，
每个任务创建独立会话，关闭文件写入、MCP、记忆和规划。

这里的隔离指工作区、会话与存储边界，不是进程沙箱。只转交明确的任务文本，不复制
全部父会话或长期记忆；任务文本本身仍可能包含上下文，应先信任对端及其模型服务。
共享预算会为委派预留请求、工具和输出额度；未知用量不会被当成免费调用。

超时只结束父端等待，对端可能仍运行；对端未退出前保持忙碌，不启动重叠的新任务。
可通过 `get_task(task_id)` 查询同一进程内的后续结果。任务索引有界且在内存中，
不提供硬取消或跨进程重启恢复；子运行自己的普通审计与回执独立保存。

## A2A 接入

可选适配器采用 [A2A 1.0 的 HTTP+JSON 绑定](https://a2a-protocol.org/v1.0.0/specification/#11-httpjsonrest-protocol-binding)
和[官方 Python SDK 1.2.2](https://github.com/a2aproject/a2a-python/releases/tag/v1.2.2)。
`a2a` extra 的 SDK 范围为 `>=1.2.2,<1.3.0`，按需安装，不增加默认内核依赖。

从源码目录安装并在两个终端使用[离线示例](../examples/a2a_demo.py)：

```bash
python -m pip install -e ".[a2a]"
python examples/a2a_demo.py serve
```

```bash
python examples/a2a_demo.py call "Describe the demo workspace"
```

示例默认使用 `TestModel`，不调用真实模型服务。只有明确给 `serve` 提供独立的
`--config` 才启用真实模型，该配置应使用单独的工作区和数据库。

`A2APeer` 只连接明确的回环 HTTP(S) 地址，校验 Agent Card 中的协议和 origin；
不跟随重定向、不接受环境代理、远程发现或带认证的对端。服务端只信任本机进程，
不提供网络身份认证，也不接受浏览器直接调用。不要对外网或局域网暴露它。

本版支持文本任务、状态查询与结果，不声称完整覆盖协议。取消、协议流式输出、推送通知、
远程文件、多模态和重启恢复不在本版范围。发送成功或 `working` 不是完成；
超时后状态可能未知，不代表取消。传给其他实现的预算 metadata 只是建议，
不能保证对方的实际费用或执行上限。观察面板的 SSE 与 A2A 流式能力是两回事。

## 只读实时观察

启动 `minimal-agent web` 后访问 `/inspector`。原生 HTML/CSS/JavaScript 与本机 SSE
展示四个视图，没有暂停、取消、记忆编辑或批准接口：

| 视图 | 显示与局限 |
|---|---|
| 运行 | 明确的开始/完成/失败状态、已报告用量、建议计划及关联子任务；不把工具完成当成整个任务完成 |
| 消息 | 观察到的原始会话 Message，包括工具消息；不展示隐藏思维链 |
| 上下文 | 经记忆/计划注入与缩减后的 SDK 提交前 Message 视图；不是完整 HTTP 请求或模型内部窗口 |
| 记忆 | 当前作用域的持久化条目快照与实时检索/候选事件；候选明确未生效 |

上下文保护以序列化消息**字节**计量，不是精确的 token 窗口。缺失、全零或无效 token
计数显示未知，不推算价格。SSE 连通不代表记忆库可读；读取故障会保留旧列表并提示过期。
CLI 审核后的列表变化随服务约 15 秒检查推送，也可手动只读刷新。

观察事件和原始内容只在有界内存窗口保留，断线时保留最后内容，重连缺口会重新读取
最近快照；被淘汰或重启前的原始观察内容不会恢复。持久化记忆另从数据库读取，
列表受最近 50 条及总字节上限约束，不代表全部记录。`tab` 和 `run` 选择保存在 URL，
不把原始消息写入浏览器本地存储。SQLite 会话、审计和回执仍是独立的归档机制。

## 开发与隐私

接口实现位于 [memory.py](../src/minimal_local_agent/memory.py)、
[planning.py](../src/minimal_local_agent/planning.py)、[peers.py](../src/minimal_local_agent/peers.py)、
[a2a_adapter.py](../src/minimal_local_agent/a2a_adapter.py) 和
[observation.py](../src/minimal_local_agent/observation.py)。模块接入不改变已有文件批准规则。

通用适配代码与公开样例可以提交；实际地址、密钥和个人默认模型只放本地忽略配置或环境变量。
记忆、会话和原始观察内容可能包含私有文件文本，需保护本地数据库与查看面板的浏览器。
详见[安全策略](../SECURITY.md)；本页的功能说明不代替版本验证记录。
