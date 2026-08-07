# 极简与本地 Agent 系统对比

调研日期：2026-08-08。信息来自各项目官方文档和源码仓库，不使用二手评测代替
事实依据。

## 一句话结论

Minimal Local Agent 不应和更完整的平台比“功能数量”，而应成为一个**小而可验证
的本地 Agent 基线**：

1. 用代码移除能力，而不是只在提示词中禁止；
2. 每次写入先校验并展示统一 diff；
3. 工具行为可以查询和导出；
4. 用同一套隔离任务实测本机模型；
5. 默认信任边界内没有 Shell、删除、网络、后台服务和隐藏编排。

## 快速对比

| 项目 | 主要目标 | 优势 | 相对本项目的代价 |
|---|---|---|---|
| Minimal Local Agent | 可验证的本地文件任务 | 工具面小、严格只读、diff 确认、SQLite 审计、本地评测 | 没有 Shell、UI、渠道、MCP、浏览器、语义记忆和多 Agent |
| [smolagents](https://huggingface.co/docs/smolagents/main/index) | 极简可编程 Agent 库 | API 简洁、模型/工具无关、CodeAgent、本地模型、MCP | 更像开发库；持久化、审计和生产隔离需要应用补齐 |
| [Qwen-Agent](https://qwenlm.github.io/Qwen-Agent/en/guide/get_started/features/) | 围绕 Qwen 的完整 Agent 能力 | Qwen 工具解析、并行/多步调用、RAG、内置工具、MCP、流式输出、Gradio | 范围更广且偏 Qwen；最小权限文件策略和持久审计由应用负责 |
| [nanobot](https://github.com/HKUDS/nanobot) | 轻量自托管个人助理 | 聊天渠道、Web UI、记忆、定时任务、MCP、Shell/Web/文件工具、子 Agent | 能力面和运维边界明显更大，生产加固需要更多配置 |
| [Goose](https://github.com/aaif-goose/goose) | 成熟的可扩展桌面/CLI Agent | 多模型提供商、桌面/CLI/API、大量 MCP 扩展、丰富权限模式 | 运行时、工具面和策略选择更多，理解成本更高 |

这些项目解决的问题并不完全相同。“代价”不等于缺陷，而是说明责任由谁承担。

## 各项目优缺点

### smolagents

优势：用很少代码组合模型和工具；支持 Ollama、Transformers 等本地模型；同时有
ToolCallingAgent 和 CodeAgent；MCP 集成方便。

局限：它是通用库，不直接提供本项目这种固定的工作区、SQLite 审计和写入确认
产品约束。其[安全代码执行指南](https://huggingface.co/docs/smolagents/main/tutorials/secure_code_execution)
也明确说明，本地 Python 执行器能限制导入和危险操作，但无法做到完全安全；更强
隔离需要 Docker 或远程沙箱。本项目选择从核心中完全排除代码执行。

### Qwen-Agent

优势：对 Qwen 工具调用格式和上下文管理支持更深入；支持并行、多步调用、RAG、
代码解释器、Web、图片、MCP、流式输出与 Gradio，并发布了
[规划评测](https://qwenlm.github.io/Qwen-Agent/en/benchmarks/)。

局限：功能面更广，也更偏向 Qwen 生态。严格的工作区最小权限、diff 审批和持久
审计仍需应用自己设计。本项目评测的不是广泛规划能力，而是当前本机模型能否可靠
使用这三个具体工具。

### nanobot

优势：已经是完整个人助理，拥有聊天渠道、Web UI、长期记忆、调度、Web/文件/
Shell 工具、MCP、子 Agent 和自动化。

局限：这些能力也扩大了密钥、后台服务、Shell 和网络的安全边界。其官方部署建议
在生产环境启用工作区限制，并在 Linux 上加强 Shell 沙箱。本项目通过“不提供这些
能力”来缩小问题，而不是尝试在核心中沙箱化它们。

### Goose

优势：原生桌面、CLI、API，多模型提供商和大量 MCP 扩展；提供 Autonomous、
Manual Approval、Smart Approval、Chat Only 等模式，以及 Always Allow、Ask
Before、Never Allow 的逐工具权限。参见官方[权限模式](https://goose-docs.ai/docs/guides/goose-permissions/)
与[工具权限](https://goose-docs.ai/docs/guides/managing-tools/tool-permissions/)。

局限：功能强大意味着更大的运行时和工具表。本项目的 `deny` 会直接省略写入工具
schema，确认范围也只覆盖一个明确副作用。Goose 的一条
[Qwen/Ollama issue](https://github.com/aaif-goose/goose/issues/6883) 报告了工具过多
时的调用退化；它只是个案而非通用基准，但说明小模型应针对实际工具表实测，不能
只假定“支持工具调用”就足够。

## 本项目自身的优缺点

优势：

- 最多四个工具，信任边界能一次看懂；
- 只读策略真正移除副作用能力；
- 人工批准前能看到具体文本变化；
- 提示、结果、用量和工具元数据保存在本地；
- 评测同时要求正确答案和成功的预期工具事件；
- 无服务器、队列、向量库、浏览器和后台进程。

不足：

- 当前只适合受限的文本文件任务；
- 全文件写入不如补丁式编辑节省 token；
- SQLite 会话历史不是语义长期记忆；
- 没有流式输出和图形界面；
- 三项评测只是 smoke test，不是统计稳健的排行榜；
- 因为不暴露进程执行，所以也没有进程/容器沙箱。

## 已完成与优化优先级

| 优先级 | 优化项 | 价值 | 约束 |
|---|---|---|---|
| v0.2 已完成 | 机械式只读模式 | 把策略变成可执行的能力边界 | 暂时只保留 `confirm` / `deny` |
| v0.2 已完成 | diff 优先的写入确认 | 让用户看到自己批准的具体变化 | diff 有长度上限，审计事件不保存全文 |
| v0.2 已完成 | 审计查看与 JSON 导出 | 无需手写 SQL 即可复核工具行为 | 默认保持纯本地 |
| v0.2 已完成 | 本地工具调用评测 | 尽早暴露模型/工具不兼容 | 明确标注为快速检查 |
| 下一步 | 补丁式编辑工具 | 降低 token 和整文件误覆盖风险 | 继续保留预览、批准与路径校验 |
| 下一步 | 对抗性和多语言评测 | 覆盖拒绝、越界、歧义和中文任务 | 必须确定、快速 |
| 后续 | 保持完整审计的流式输出 | 改善交互延迟 | 最终历史必须完整落库 |
| 后续 | 明确白名单的只读 MCP | 支持经过验证的外部集成 | 不做隐式发现和默认写权限 |

## 保持差异化的准入原则

“极简”应当是持续约束，而不是暂时功能少。每个新增能力都必须回答：

1. 当前工具无法完成哪个已测量的任务？
2. 完成它所需的最小权限是什么？
3. 如何测试、限制并审计它？

如果答案不清楚，该功能更适合放在可选适配器或更完整的 Agent 平台，而不是核心。
