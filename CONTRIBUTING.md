# 参与开发

感谢你愿意改进 Minimal Local Agent。这份说明介绍开发环境、提交要求和几条内核约定。

## 开发环境

需要 Python 3.11+。

```bash
git clone https://github.com/MuzeAnisichael/minimal-local-agent.git
cd minimal-local-agent
python -m venv .venv && source .venv/bin/activate   # Windows：.venv\Scripts\activate
pip install -e ".[dev,mcp]"
```

## 提交前检查

和 CI 一致，下面四项都要通过：

```bash
ruff format --check .
ruff check .
pytest
python -m minimal_local_agent eval --offline --out eval-report.json
```

测试和离线评测都使用脚本模型，不需要网络和 API 密钥。

## 内核约定

- **所有工具走 `ToolExecutor`。** 不要在循环里直接调用工具函数，否则账本、预算、超时、取消、事件都会被绕过。
- **写工具先 prepare，再 apply。** 新的副作用要实现 `EffectHandler` 的 `apply` / `probe` / `compensate`，
  `prepare` 返回的数据必须可序列化，恢复和补偿只能依赖账本里的这份数据。
- **恢复所需的状态只放在状态库里。** 进程内缓存可以有，但不能是续跑必需的。
- **行为改动要有确定性的测试。** 涉及中断、恢复的改动，优先在 `evals/families/` 里加一个带故障的用例。
- **不提交密钥和本地数据。** `agent.toml`、`.env`、`.mla/`、`workspace/` 都已被忽略。

## 提交与 PR

- 提交信息用“类型: 说明”的格式，例如 `feat: ...`、`fix: ...`、`docs: ...`、`test: ...`、`refactor: ...`。
- PR 按模板填写改动说明和验证结果；用户可见的改动请同时更新 `CHANGELOG.md`。
- 中文为主：注释、文档、命令行输出用中文；代码标识符、配置键、事件名用英文。

## 发布

1. 更新 `pyproject.toml` 的版本号和 `CHANGELOG.md`。
2. 在 `docs/releases/v<版本>.md` 写发布说明（第一行 `## 标题：副标题`，副标题会成为 Release 标题）。
3. 合并到 `main` 后推送标签 `v<版本>`，`Release` 工作流会测试、构建 sdist / wheel 并创建 GitHub Release。
   已有标签也可以在 Actions 页面手动运行 `Release` 工作流补发。
