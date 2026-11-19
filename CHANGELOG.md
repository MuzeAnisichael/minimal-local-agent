# 更新记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
每个版本的完整说明在 [`docs/releases/`](docs/releases/)。

## 未发布

- 新增：`examples/offline_demo.py`，不需要模型就能演示崩溃续跑、对账和产物回溯。
- 新增：发布工作流，推送 `v*` 标签时测试、构建 sdist / wheel 并创建 GitHub Release。
- 文档：重写中文 README，新增英文 README、评测说明、配置说明、贡献指南和 Issue 模板。

## 2.0.0（2026-11-19）

[发布说明](docs/releases/v2.0.0.md)

执行内核整体重写，不兼容 1.x 的接口、配置和数据库。旧版代码保存在 `v1.1-archive` 标签（即 1.x 最后一次提交）。

- 运行时自己掌握“规划、执行、校验”循环，PydanticAI 只作为模型调用层。
- 新增：持久化检查点、幂等工具账本、文件对账与补偿、跨进程取消、工具超时与重试、可回溯的事件轨迹。
- 新增：按角色的模型路由与回退、token 级上下文预算与压缩、子任务、stdio/HTTP MCP 接入、记忆策略。
- 新增：按任务族的评测，支持故障注入和策略对比（成功率、故障恢复率、每成功任务成本）。
- 新增：真实模型评测（DeepSeek、OpenRouter 上的 Qwen3-Coder），结果见 README。
- 移除：Web 控制台、实时观察面板、A2A 适配、记忆人工审核流程、回执哈希链。

## 1.x

1.x 的更新记录和代码见 [`v1.1-archive`](https://github.com/MuzeAnisichael/minimal-local-agent/tree/v1.1-archive) 标签。
