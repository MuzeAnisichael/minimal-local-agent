# 文档导航

[English](README.md) · [项目首页](../README.zh-CN.md)

先完成[快速开始](../README.zh-CN.md#快速开始)，再按目标选择说明。文档直接保存在仓库内，
不需要额外部署文档服务。部分开发参考目前为英文，已在下方标明。

| 我想…… | 对应说明 |
|---|---|
| 运行 CLI 任务或使用本地 Web | [使用指南](GUIDE.zh-CN.md#cli-与-web) |
| 配置本地模型或兼容 API | [配置](GUIDE.zh-CN.md#配置) |
| 添加只读工具、模型工厂或上下文缩减策略 | [嵌入与扩展](GUIDE.zh-CN.md#嵌入与扩展)、[可运行示例](../examples/read_tool.py) |
| 不碰个人文件，体验界面 | [临时工作区演示](../examples/web_demo.py)、[截图来源（英文）](assets/README.md) |
| 理解实现与权限边界 | [架构（英文）](ARCHITECTURE.md)、[安全策略（英文）](../SECURITY.md) |
| 升级已有项目或编写集成 | [v1 兼容与迁移约定（英文）](COMPATIBILITY.md) |
| 评测工具调用与文件结果 | [评测](GUIDE.zh-CN.md#评测)、[验证记录（英文）](VALIDATION.md)、[发行摘要](../evals/results/v1.0.json) |
| 比较项目范围或建议新功能 | [同类对比](COMPARISON.zh-CN.md)、[路线图](ROADMAP.zh-CN.md) |
| 报告问题、贡献或查看变更 | [贡献说明（英文）](../CONTRIBUTING.md)、[更新日志（英文）](../CHANGELOG.md) |

## 怎样理解验证结果

v1.0 发行验证通过了 91 项自动化测试，并在 Ollama 与一种兼容 API 上完成小型真实回归。
这不是模型排行榜或统计意义上的可靠性保证。预算、局限与未知用量均记录在
[VALIDATION.md](VALIDATION.md) 中。

1.x 保持文档约定的扩展与数据兼容。路线图已转为维护优先，不承诺自动压缩或大型编排。
